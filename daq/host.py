"""Represent a device-under-test"""

import os
import shutil
import time
from datetime import timedelta, datetime

from clib import tcpdump_helper

import configurator
import docker_test
import gcp
import report
import logger

LOGGER = logger.get_logger('host')


class _STATE:
    """Host state enum for testing cycle"""
    ERROR = 'Error condition'
    READY = 'Ready but not initialized'
    INIT = 'Initialization'
    WAITING = 'Waiting for activation'
    BASE = 'Baseline tests'
    MONITOR = 'Network monitor'
    NEXT = 'Ready for next'
    TESTING = 'Active test'
    DONE = 'Done with sequence'
    TERM = 'Host terminated'


class MODE:
    """Test module modes for state reporting."""
    INIT = 'init'
    PREP = 'prep'
    HOLD = 'hold'
    CONF = 'conf'
    EXEC = 'exec'
    FINE = 'fine'
    NOPE = 'nope'
    HOLD = 'hold'
    DONE = 'done'
    TERM = 'term'
    LONG = 'long'
    MERR = 'merr'


def pre_states():
    """Return pre-test states for basic operation"""
    return ['startup', 'sanity', 'ipaddr', 'base', 'monitor']


def post_states():
    """Return post-test states for recording finalization"""
    return ['finish', 'info', 'timer']


class ConnectedHost:
    """Class managing a device-under-test"""

    _MONITOR_SCAN_SEC = 30
    _DEFAULT_TIMEOUT = 350
    _STARTUP_MIN_TIME_SEC = 5
    _INST_DIR = "inst/"
    _DEVICE_PATH = "device/%s"
    _FAIL_BASE_FORMAT = "inst/fail_%s"
    _MODULE_CONFIG = "module_config.json"
    _CONTROL_PATH = "control/port-%s"
    _CORE_TESTS = ['pass', 'fail', 'ping', 'hold']
    _AUX_DIR = "aux/"
    _CONFIG_DIR = "config/"
    _TIMEOUT_EXCEPTION = TimeoutError('Timeout expired')

    def __init__(self, runner, gateway, target, config):
        self.runner = runner
        self._gcp = runner.gcp
        self.gateway = gateway
        self.config = config
        self.target_port = target['port']
        self.target_mac = target['mac']
        self.fake_target = target['fake']
        self.devdir = self._init_devdir()
        self.run_id = self.make_runid()
        self.scan_base = os.path.abspath(os.path.join(self.devdir, 'scans'))
        self._port_base = self._get_port_base()
        self._device_base = self._get_device_base()
        self.state = None
        self._state_transition(_STATE.READY)
        self.results = {}
        self.dummy = None
        self.test_name = None
        self.test_start = None
        self.test_host = None
        self.test_port = None
        self._startup_time = None
        self._monitor_scan_sec = int(config.get('monitor_scan_sec', self._MONITOR_SCAN_SEC))
        _default_timeout_sec = int(config.get('default_timeout_sec', self._DEFAULT_TIMEOUT))
        self._default_timeout_sec = _default_timeout_sec if _default_timeout_sec else None
        self._fail_hook = config.get('fail_hook')
        self._mirror_intf_name = None
        self._tcp_monitor = None
        self.target_ip = None
        self._loaded_config = None
        self.reload_config()
        self._dhcp_listeners = []
        configurator.write_config(self._device_aux_path(), self._MODULE_CONFIG, self._loaded_config)
        assert self._loaded_config, 'config was not loaded'
        self.remaining_tests = self._get_enabled_tests()
        LOGGER.info('Host %s running with enabled tests %s', self.target_port, self.remaining_tests)

        self.record_result('startup', state=MODE.PREP)
        self._record_result('info', state=self.target_mac, config=self._make_config_bundle())
        self._report = report.ReportGenerator(config, self._INST_DIR, self.target_mac,
                                              self._loaded_config)
        self.timeout_handler = self._aux_module_timeout_handler
    @staticmethod
    def make_runid():
        """Create a timestamped runid"""
        return '%06x' % int(time.time())

    def _init_devdir(self):
        devdir = os.path.join(self._INST_DIR, 'run-port-%02d' % self.target_port)
        shutil.rmtree(devdir, ignore_errors=True)
        os.makedirs(devdir)
        return devdir

    def _get_port_base(self):
        test_config = self.config.get('test_config')
        if not test_config:
            return None
        conf_base = os.path.abspath(os.path.join(test_config, 'port-%02d' % self.target_port))
        if not os.path.isdir(conf_base):
            LOGGER.warning('Test config directory not found: %s', conf_base)
            return None
        return conf_base

    def _make_config_bundle(self, config=None):
        return {
            'config': config if config else self._loaded_config,
            'timestamp': gcp.get_timestamp()
        }

    def _make_control_bundle(self):
        return {
            'paused': self.state == _STATE.READY
        }

    def _test_enabled(self, test):
        fallback_config = {'enabled': test in self._CORE_TESTS}
        test_config = self._loaded_config['modules'].get(test, fallback_config)
        return test_config.get('enabled', True)

    def _get_test_timeout(self, test):
        test_module = self._loaded_config['modules'].get(test)
        return test_module.get('timeout_sec', self._default_timeout_sec) if test_module else None

    def _get_enabled_tests(self):
        return list(filter(self._test_enabled, self.config.get('test_list')))

    def _get_device_base(self):
        """Get the base config path for a host device"""
        site_path = self.config.get('site_path')
        if not site_path:
            return None
        clean_mac = self.target_mac.replace(':', '')
        dev_path = os.path.abspath(os.path.join(site_path, 'mac_addrs', clean_mac))
        if not os.path.isdir(dev_path):
            self._create_device_dir(dev_path)
        return dev_path

    def _get_static_ip(self):
        return self._loaded_config.get('static_ip')

    def _type_path(self):
        dev_config = configurator.load_config(self._device_base, self._MODULE_CONFIG)
        device_type = dev_config.get('device_type')
        if not device_type:
            return None
        LOGGER.info('Configuring device %s as type %s', self.target_mac, device_type)
        site_path = self.config.get('site_path')
        type_path = os.path.abspath(os.path.join(site_path, 'device_types', device_type))
        return type_path

    def _type_aux_path(self):
        type_path = self._type_path()
        if not type_path:
            return None
        aux_path = os.path.join(type_path, self._AUX_DIR)
        if not os.path.exists(aux_path):
            LOGGER.info('Skipping missing type dir %s', aux_path)
            return None
        return aux_path

    def _create_device_dir(self, path):
        LOGGER.warning('Creating new device dir: %s', path)
        os.makedirs(path)
        template_dir = self.config.get('device_template')
        if not template_dir:
            LOGGER.warning('Skipping defaults since no device_template found')
            return
        LOGGER.info('Copying template files from %s to %s', template_dir, path)
        for file in os.listdir(template_dir):
            LOGGER.info('Copying %s...', file)
            shutil.copy(os.path.join(template_dir, file), path)

    def initialize(self):
        """Fully initialize a new host set"""
        LOGGER.info('Target port %d initializing...', self.target_port)
        # There is a race condition here with ovs assigning ports, so wait a bit.
        time.sleep(2)
        shutil.rmtree(self.devdir, ignore_errors=True)
        os.makedirs(self.scan_base)
        self._initialize_config()
        network = self.runner.network
        self._mirror_intf_name = network.create_mirror_interface(self.target_port)
        if self.config['test_list']:
            self._start_run()
        else:
            assert self.is_holding(), 'state is not holding'
            self.record_result('startup', state=MODE.HOLD)

    def _start_run(self):
        self._state_transition(_STATE.INIT, _STATE.READY)
        self._mark_skipped_tests()
        self.record_result('startup', state=MODE.DONE, config=self._make_config_bundle())
        self.record_result('sanity', state=MODE.EXEC)
        self._startup_scan()

    def _mark_skipped_tests(self):
        for test in self.config['test_list']:
            if not self._test_enabled(test):
                self._record_result(test, state=MODE.NOPE)

    def _state_transition(self, target, expected=None):
        if expected is not None:
            message = 'state was %s expected %s' % (self.state, expected)
            assert self.state == expected, message
        LOGGER.debug('Target port %d state: %s -> %s', self.target_port, self.state, target)
        self.state = target

    def is_running(self):
        """Return True if this host is running active test."""
        return self.state != _STATE.ERROR and self.state != _STATE.DONE

    def is_holding(self):
        """Return True if this host paused and waiting to run."""
        return self.state == _STATE.READY

    def notify_activate(self):
        """Return True if ready to be activated in response to an ip notification."""
        if self.state == _STATE.READY:
            self._record_result('startup', state=MODE.HOLD)
        return self.state == _STATE.WAITING

    def _prepare(self):
        LOGGER.info('Target port %d waiting for ip as %s', self.target_port, self.target_mac)
        self._state_transition(_STATE.WAITING, _STATE.INIT)
        self.record_result('sanity', state=MODE.DONE)
        self.record_result('ipaddr', state=MODE.EXEC)
        static_ip = self._get_static_ip()
        _ = [listener(self) for listener in self._dhcp_listeners]
        if static_ip:
            time.sleep(self._STARTUP_MIN_TIME_SEC)
            self.runner.ip_notify(MODE.DONE, {
                'mac': self.target_mac,
                'ip': static_ip,
                'delta': -1
            }, self.gateway.port_set)

    def _aux_module_timeout_handler(self):
        # clean up tcp monitor that could be open
        self._monitor_error(self._TIMEOUT_EXCEPTION, forget=True)

    def _main_module_timeout_handler(self):
        self.test_host.terminate()
        self.test_host = None
        self._docker_callback(exception=self._TIMEOUT_EXCEPTION)

    def check_module_timeout(self):
        """Checks module run time for each event loop"""
        timeout_sec = self._get_test_timeout(self.test_name)
        if not timeout_sec or not self.test_start:
            return
        delta_sec = timedelta(seconds=timeout_sec)
        timeout = gcp.parse_timestamp(self.test_start) + delta_sec
        if  datetime.fromtimestamp(time.time()) >= timeout:
            if self.timeout_handler:
                LOGGER.error('Monitoring timeout for %s after %ds', self.test_name, timeout_sec)
                # ensure it's called once
                handler, self.timeout_handler = self.timeout_handler, None
                handler()

    def register_dhcp_ready_listener(self, callback):
        """Registers callback for when the host is ready for activation"""
        assert callable(callback), "ip listener callback is not callable"
        self._dhcp_listeners.append(callback)

    def terminate(self, reason, trigger=True):
        """Terminate this host"""
        LOGGER.info('Target port %d terminate, running %s, trigger %s: %s', self.target_port,
                    self._host_name(), trigger, reason)
        self._release_config()
        self._state_transition(_STATE.TERM)
        self.record_result(self.test_name, state=MODE.TERM)
        self._monitor_cleanup()
        self.runner.network.delete_mirror_interface(self.target_port)
        if self.test_host:
            try:
                self.test_host.terminate(expected=trigger)
                self.test_host = None
            except Exception as e:
                LOGGER.error('Target port %d terminating test: %s', self.target_port, e)
                LOGGER.exception(e)
        if trigger:
            self.runner.target_set_complete(self.target_port,
                                            'Target port %d termination: %s' % (
                                                self.target_port, self.test_host))

    def idle_handler(self):
        """Trigger events from idle state"""
        if self.state == _STATE.INIT:
            self._prepare()
        elif self.state == _STATE.BASE:
            self._base_start()

    def trigger_ready(self):
        """Check if this host is ready to be triggered"""
        if self.state != _STATE.WAITING:
            return False
        delta_t = datetime.now() - self._startup_time
        if delta_t < timedelta(seconds=self._STARTUP_MIN_TIME_SEC):
            return False
        return True

    def trigger(self, state=MODE.DONE, target_ip=None, exception=None, delta_sec=-1):
        """Handle completion of ip subtask"""
        trigger_path = os.path.join(self.scan_base, 'ip_triggers.txt')
        with open(trigger_path, 'a') as output_stream:
            output_stream.write('%s %s %d\n' % (target_ip, state, delta_sec))
        if self.target_ip:
            LOGGER.debug('Target port %d already triggered', self.target_port)
            assert self.target_ip == target_ip, "target_ip mismatch"
            return True
        if not self.trigger_ready():
            LOGGER.warning('Target port %d ignoring premature trigger', self.target_port)
            return False
        self.target_ip = target_ip
        self._record_result('info', state='%s/%s' % (self.target_mac, target_ip))
        self.record_result('ipaddr', ip=target_ip, state=state, exception=exception)
        if exception:
            self._state_transition(_STATE.ERROR)
            self.runner.target_set_error(self.target_port, exception)
        else:
            LOGGER.info('Target port %d triggered as %s', self.target_port, target_ip)
            self._state_transition(_STATE.BASE, _STATE.WAITING)
        return True

    def _ping_test(self, src, dst, src_addr=None):
        if not src or not dst:
            LOGGER.error('Invalid ping test params, src=%s, dst=%s', src, dst)
            return False
        return self.runner.ping_test(src, dst, src_addr=src_addr)

    def _startup_scan(self):
        assert not self._tcp_monitor, 'tcp_monitor already active'
        startup_file = os.path.join(self.scan_base, 'startup.pcap')
        self._startup_time = datetime.now()
        LOGGER.info('Target port %d startup pcap capture', self.target_port)
        network = self.runner.network
        tcp_filter = ''
        LOGGER.debug('Target port %d startup scan intf %s filter %s output in %s',
                     self.target_port, self._mirror_intf_name, tcp_filter, startup_file)
        helper = tcpdump_helper.TcpdumpHelper(network.pri, tcp_filter, packets=None,
                                              intf_name=self._mirror_intf_name,
                                              timeout=None, pcap_out=startup_file, blocking=False)
        self._tcp_monitor = helper
        hangup = lambda: self._monitor_error(Exception('startup scan hangup'))
        self.runner.monitor_stream('tcpdump', self._tcp_monitor.stream(),
                                   self._tcp_monitor.next_line,
                                   hangup=hangup, error=self._monitor_error)

    def _base_start(self):
        try:
            success = self._base_tests()
            self._monitor_cleanup()
            if not success:
                LOGGER.warning('Target port %d base tests failed', self.target_port)
                self._state_transition(_STATE.ERROR)
                return
            LOGGER.info('Target port %d done with base.', self.target_port)
            self._monitor_scan()
        except Exception as e:
            self._monitor_cleanup()
            self._monitor_error(e)

    def _monitor_cleanup(self, forget=True):
        if self._tcp_monitor:
            LOGGER.info('Target port %d monitor scan complete', self.target_port)
            nclosed = self._tcp_monitor.stream() and not self._tcp_monitor.stream().closed
            assert nclosed == forget, 'forget and nclosed mismatch'
            if forget:
                self.runner.monitor_forget(self._tcp_monitor.stream())
                self._tcp_monitor.terminate()
            self._tcp_monitor = None

    def _monitor_error(self, exception, forget=False):
        LOGGER.error('Target port %d monitor error: %s', self.target_port, exception)
        self._monitor_cleanup(forget=forget)
        self.record_result(self.test_name, exception=exception)
        self._state_transition(_STATE.ERROR)
        self.runner.target_set_error(self.target_port, exception)

    def _monitor_scan(self):
        self._state_transition(_STATE.MONITOR, _STATE.BASE)
        if not self._monitor_scan_sec:
            LOGGER.info('Target port %d skipping background scan', self.target_port)
            self._monitor_continue()
            return
        self.record_result('monitor', time=self._monitor_scan_sec, state=MODE.EXEC)
        monitor_file = os.path.join(self.scan_base, 'monitor.pcap')
        LOGGER.info('Target port %d background scan for %ds',
                    self.target_port, self._monitor_scan_sec)
        network = self.runner.network
        tcp_filter = ''
        intf_name = self._mirror_intf_name
        assert not self._tcp_monitor, 'tcp_monitor already active'
        LOGGER.debug('Target port %d background scan intf %s filter %s output in %s',
                     self.target_port, intf_name, tcp_filter, monitor_file)
        helper = tcpdump_helper.TcpdumpHelper(network.pri, tcp_filter, packets=None,
                                              intf_name=intf_name,
                                              timeout=self._monitor_scan_sec,
                                              pcap_out=monitor_file, blocking=False)
        self._tcp_monitor = helper
        self.runner.monitor_stream('tcpdump', self._tcp_monitor.stream(),
                                   self._tcp_monitor.next_line, hangup=self._monitor_complete,
                                   error=self._monitor_error)

    def _monitor_complete(self):
        LOGGER.info('Target port %d scan complete', self.target_port)
        self._monitor_cleanup(forget=False)
        self.record_result('monitor', state=MODE.DONE)
        self._monitor_continue()

    def _monitor_continue(self):
        self._state_transition(_STATE.NEXT, _STATE.MONITOR)
        self._run_next_test()

    def _base_tests(self):
        self.record_result('base', state=MODE.EXEC)
        if not self._ping_test(self.gateway.host, self.target_ip):
            LOGGER.debug('Target port %d warmup ping failed', self.target_port)
        try:
            success1 = self._ping_test(self.gateway.host, self.target_ip), 'simple ping failed'
            success2 = self._ping_test(self.gateway.host, self.target_ip,
                                       src_addr=self.fake_target), 'target ping failed'
            if not success1 or not success2:
                return False
        except Exception as e:
            self.record_result('base', exception=e)
            self._monitor_cleanup()
            raise
        self.record_result('base', state=MODE.DONE)
        return True

    def _run_next_test(self):
        try:
            if self.remaining_tests:
                self.timeout_handler = self._main_module_timeout_handler
                self._docker_test(self.remaining_tests.pop(0))
            else:
                self.timeout_handler = self._aux_module_timeout_handler
                LOGGER.info('Target port %d no more tests remaining', self.target_port)
                self._state_transition(_STATE.DONE, _STATE.NEXT)
                self._report.finalize()
                self._gcp.upload_report(self._report.path)
                self.record_result('finish', state=MODE.FINE, report=self._report.path)
                self._report = None
                self.record_result(None)
        except Exception as e:
            LOGGER.error('Target port %d start error: %s', self.target_port, e)
            self._state_transition(_STATE.ERROR)
            self.runner.target_set_error(self.target_port, e)

    def _inst_config_path(self):
        return os.path.abspath(os.path.join(self._INST_DIR, self._CONFIG_DIR))

    def _device_aux_path(self):
        path = os.path.join(self._device_base, self._AUX_DIR)
        if not os.path.exists(path):
            os.makedirs(path)
        return path

    def _docker_test(self, test_name):
        self.test_name = test_name
        self.test_start = gcp.get_timestamp()
        self._state_transition(_STATE.TESTING, _STATE.NEXT)
        params = {
            'target_ip': self.target_ip,
            'target_mac': self.target_mac,
            'gateway_ip': self.gateway.host.IP(),
            'gateway_mac': self.gateway.host.MAC(),
            'inst_base': self._inst_config_path(),
            'port_base': self._port_base,
            'device_base': self._device_aux_path(),
            'type_base': self._type_aux_path(),
            'scan_base': self.scan_base
        }
        self.test_host = docker_test.DockerTest(self.runner, self.target_port, self.devdir,
                                                self.test_name)
        self.test_port = self.runner.allocate_test_port(self.target_port)
        if 'ext_loip' in self.config:
            ext_loip = self.config['ext_loip'].replace('@', '%d')
            params['local_ip'] = ext_loip % self.test_port
            params['switch_ip'] = self.config['ext_addr']
            params['switch_port'] = str(self.target_port)
            params['switch_model'] = self.config['switch_model']

        try:
            LOGGER.debug('test_host start %s/%s', self.test_name, self._host_name())
            self._set_module_config(self._loaded_config)
            self.record_result(test_name, state=MODE.EXEC)
            self.test_host.start(self.test_port, params, self._docker_callback)
        except:
            self.test_host = None
            raise

    def _host_name(self):
        return self.test_host.host_name if self.test_host else 'unknown'

    def _host_dir_path(self):
        return os.path.join(self.devdir, 'nodes', self._host_name())

    def _host_tmp_path(self):
        return os.path.join(self._host_dir_path(), 'tmp')

    def _docker_callback(self, return_code=None, exception=None):
        self.timeout_handler = None # cancel timeout handling
        host_name = self._host_name()
        LOGGER.info('Host callback %s/%s was %s with %s',
                    self.test_name, host_name, return_code, exception)
        failed = return_code or exception
        if failed and self._fail_hook:
            fail_file = self._FAIL_BASE_FORMAT % host_name
            LOGGER.warning('Executing fail_hook: %s %s', self._fail_hook, fail_file)
            os.system('%s %s 2>&1 > %s.out' % (self._fail_hook, fail_file, fail_file))
        state = MODE.MERR if failed else MODE.DONE
        self.record_result(self.test_name, state=state, code=return_code, exception=exception)
        result_path = os.path.join(self._host_dir_path(), 'return_code.txt')
        try:
            with open(result_path, 'a') as output_stream:
                output_stream.write(str(return_code) + '\n')
        except Exception as e:
            LOGGER.error('While writing result code: %s', e)
        report_path = os.path.join(self._host_tmp_path(), 'report.txt')
        if os.path.isfile(report_path):
            self._report.accumulate(self.test_name, report_path)
        self.runner.release_test_port(self.target_port, self.test_port)
        self._state_transition(_STATE.NEXT, _STATE.TESTING)
        self._run_next_test()

    def _set_module_config(self, loaded_config):
        tmp_dir = self._host_tmp_path()
        configurator.write_config(tmp_dir, self._MODULE_CONFIG, loaded_config)
        self._record_result(self.test_name, config=self._loaded_config, state=MODE.CONF)

    def _merge_run_info(self, config):
        config['run_info'] = {
            'run_id': self.run_id,
            'mac_addr': self.target_mac,
            'daq_version': self.runner.version,
            'started': gcp.get_timestamp()
        }

    def _load_module_config(self, run_info=True):
        config = self.runner.get_base_config()
        if run_info:
            self._merge_run_info(config)
        configurator.load_and_merge(config, self._type_path(), self._MODULE_CONFIG)
        configurator.load_and_merge(config, self._device_base, self._MODULE_CONFIG)
        configurator.load_and_merge(config, self._port_base, self._MODULE_CONFIG)
        return config

    def record_result(self, name, **kwargs):
        """Record a named result for this test"""
        current = gcp.get_timestamp()
        if name != self.test_name:
            LOGGER.debug('Target port %d report %s start %s',
                         self.target_port, name, current)
            self.test_name = name
            self.test_start = current
        if name:
            self._record_result(name, current, **kwargs)

    @staticmethod
    def clear_port(gcp_instance, port):
        """Clear a port-based entry without having an instantiated host class"""
        result = {
            'name': 'startup',
            'state': MODE.INIT,
            'runid': ConnectedHost.make_runid(),
            'timestamp': gcp.get_timestamp(),
            'port': port
        }
        gcp_instance.publish_message('daq_runner', 'test_result', result)

    def _record_result(self, name, run_info=True, current=None, **kwargs):
        result = {
            'name': name,
            'runid': (self.run_id if run_info else None),
            'device_id': self.target_mac,
            'started': self.test_start,
            'timestamp': current if current else gcp.get_timestamp(),
            'port': (self.target_port if run_info else None)
        }
        result.update(kwargs)
        if 'exception' in result:
            result['exception'] = self._exception_message(result['exception'])
        if name:
            self.results[name] = result
        self._gcp.publish_message('daq_runner', 'test_result', result)
        return result

    def _exception_message(self, exception):
        if not exception or exception == 'None':
            return None
        if isinstance(exception, Exception):
            return exception.__class__.__name__
        return str(exception)

    def _control_updated(self, control_config):
        LOGGER.info('Updated control config: %s %s', self.target_mac, control_config)
        paused = control_config.get('paused')
        if not paused and self.is_holding():
            self._start_run()
        elif paused and not self.is_holding():
            LOGGER.warning('Inconsistent control state for update of %s', self.target_mac)

    def reload_config(self):
        """Trigger a config reload due to an eternal config change."""
        holding = self.is_holding()
        new_config = self._load_module_config(run_info=holding)
        if holding:
            self._loaded_config = new_config
        config_bundle = self._make_config_bundle(new_config)
        LOGGER.info('Device config reloaded: %s %s', holding, self.target_mac)
        self._record_result(None, run_info=holding, config=config_bundle)
        return new_config

    def _dev_config_updated(self, dev_config):
        LOGGER.info('Device config update: %s %s', self.target_mac, dev_config)
        configurator.write_config(self._device_base, self._MODULE_CONFIG, dev_config)
        self.reload_config()

    def _initialize_config(self):
        dev_config = configurator.load_config(self._device_base, self._MODULE_CONFIG)
        self._gcp.register_config(self._DEVICE_PATH % self.target_mac,
                                  dev_config, self._dev_config_updated)
        self._gcp.register_config(self._CONTROL_PATH % self.target_port,
                                  self._make_control_bundle(),
                                  self._control_updated, immediate=True)
        self._record_result(None, config=self._make_config_bundle())

    def _release_config(self):
        self._gcp.release_config(self._DEVICE_PATH % self.target_mac)
        self._gcp.release_config(self._CONTROL_PATH % self.target_port)
