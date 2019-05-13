# Copyright (C) 2019 Red Hat, Inc., Jake Hunsaker <jhunsake@redhat.com>

# This file is part of the rig project: https://github.com/TurboTurtle/rig
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

import ast
import inspect
import json
import logging
import os
import random
import string
import socket
import sys

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from concurrent.futures import thread
from rigging.exceptions import *

RIG_DIR = '/var/run/rig/'
RIG_TMP_DIR = '/var/tmp/rig/'


class BaseRig():
    '''
    Base class for resources/triggers.

    Will build a set of argparse options based upon what the rig provides by
    itself, as well as the defined actions it supports.

    Two class members must be defined by a rig:
        resource_name: The canonical name of the rig as used to invoke it
                       on the commandline.
        parser_description: The argparse description text for the Rig's submenu
        parser_usage:  The argparse usage text for the Rig's submenu
    '''

    parser_description = None
    parser_usage = '''
    rig %(name)s <options>

    Valid actions:

    '''
    triggered = False
    watcher_threads = []

    def __init__(self, parser):
        self.detached = False
        self._status = 'Initializing'
        self.resource_name = self.__class__.__name__.lower()
        self.parser_usage = self.parser_usage % {'name': self.resource_name}
        self.pool = None
        self.parser = parser
        subparser = self.parser.add_subparsers()
        self.rig_parser = subparser.add_parser(self.resource_name)
        self.rig_parser = self._setup_parser(self.rig_parser)

        self.id = (''.join(random.choice(string.ascii_lowercase)
                   for x in range(5)))

        self.supported_actions = self._load_supported_actions()
        for action in self.supported_actions:
            self.supported_actions[action].add_action_options(self.rig_parser)
            self.parser_usage += '\t{:<15} {:>30}\n'.format(
                self.supported_actions[action].enabling_opt,
                self.supported_actions[action].enabling_opt_desc
            )

        if self.parser_description:
            self.rig_parser.description = self.parser_description
        if self.parser_usage:
            self.rig_parser.usage = self.parser_usage

        self._can_run = self._load_args()
        if self._can_run:
            self._setup_rig_logging()
            self._sock, self._sock_address = self._create_rig_socket()
            self._tmp_dir = self._create_temp_dir()
            self.files = []

    def _exit(self, errno):
        '''
        Handles pre-mature exits due to errors
        '''
        self._cleanup()
        raise SystemExit(errno)

    def _detach(self):
        '''
        Here we effectively daemonize the process by using the double-fork
        method. The rig will continue to run until a trigger event, or until
        the rig cli is used to send a termination signal to the socket the rig
        is listening on.
        '''
        def _fork():
            try:
                pid = os.fork()
                if pid > 0:
                    sys.exit(0)
            except OSError as e:
                self.log_error("Fork failed: %s" % e)
                sys.exit(1)
        _fork()
        os.setsid()
        os.umask(0)
        _fork()

        sys.stdin = sys.__stdin__
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        sys.stdout.flush()
        sys.stderr.flush()
        _std = getattr(os, 'devnull', '/dev/null')
        sin = open(_std, 'r')
        sout = open(_std, 'a+')
        serr = open(_std, 'a+')
        os.dup2(sin.fileno(), sys.stdin.fileno())
        os.dup2(sout.fileno(), sys.stdin.fileno())
        os.dup2(serr.fileno(), sys.stderr.fileno())

        self.pid = os.getpid()
        return True

    def _create_rig_socket(self):
        '''
        Creates the UNIX socket that the rig will listen on for lifecycle
        management.

        This socket is used by the rig cli when getting status information or
        destroying a deployed rig before the trigger event happens.
        '''
        if not os.path.exists(RIG_DIR):
            os.makedirs(RIG_DIR)
        _sock_address = "%s%s" % (RIG_DIR, self.id)
        try:
            os.unlink(_sock_address)
        except OSError:
            if os.path.exists(_sock_address):
                raise SocketExistsError(_sock_address)
        try:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _sock.bind(_sock_address)
            _sock.listen(1)
            return (_sock, _sock_address)
        except Exception as err:
            self.log_error("Unable to create unix socket: %s" % err)
            raise CreateSocketException

    def _create_temp_dir(self):
        '''
        Create a temp directory for rig to use for saving created files too
        '''
        try:
            _dir = RIG_TMP_DIR + self.id + '/'
            os.makedirs(_dir)
            return _dir
        except OSError:
            raise CannotConfigureRigError('failed to create temp directory')

    def _load_args(self):
        '''
        Parses the args given to us by the user.

        This is called while trying to load a rig. If we do not have any args,
        then that means that '--help' was called, in which case we return False
        to ensure we don't begin logging for no reason.

        If there is an unknown option provided, argparse appends it to another
        namespace list, thus if this list contains more than just the resource
        as an element, it means we have an unknown arg.
        '''
        args = self.rig_parser.parse_known_args()
        filt = ['--debug', '--foreground']
        self.debug = '--debug' in args[1]
        self.foreground = '--foreground' in args[1]
        unknowns = [x for x in args[1][1:] if x not in filt]
        if len(unknowns):
            print("Unknown option %s specified." %
                  unknowns[0].split('=')[0])
            return False
        self.args = vars(self.rig_parser.parse_known_args()[0])
        if self.args:
            return True
        return False

    def _load_supported_actions(self):
        '''
        Looks at the defined actions available to rig, and if they match any
        of the strings listed in supported_actions instantiates them.
        '''
        actions = {}
        import rigging.actions
        pkg = rigging.actions
        for path in pkg.__path__:
            if os.path.isdir(path):
                for pyfile in sorted(os.listdir(path)):
                    if not pyfile.endswith('.py') or '__' in pyfile:
                        continue
                    fname, ext = os.path.splitext(pyfile)
                    modname = 'rigging.actions.%s' % fname
                    mod_short_name = modname.split('.')[2]
                    mod = __import__(modname, globals(), locals(),
                                     [mod_short_name])
                    module = inspect.getmembers(mod, inspect.isclass)[1]
                    actions[module[1].action_name] = module[1](self.rig_parser,
                                                               self.id)
        return actions

    def _setup_parser(self, parser):
        '''
        Builds the option parser based on supported actions, then appends the
        rig-specific options.

        Returns:
            parser: an ArgumentParser object that contains the rig-specific
                    options.
        '''
        return self.set_parser_options(parser)

    def compile_details(self):
        try:
            args = sys.argv[2:]
            return ' '.join(args)[:40]
        except Exception:
            return ''

    @property
    def watching(self):
        '''
        MUST be overridden by rigs. This should return a string describing
        what resource(s) the rig is monitoring
        '''
        return NotImplementedError

    @property
    def trigger(self):
        '''
        MUST be overridden by rigs. This should return a string containing the
        trigger event for the monitored resource.
        '''
        return NotImplementedError

    @property
    def status(self, value=None):
        '''
        Returns the current status of the rig.

        :param value: Unused, but passed by the listening socket
        '''
        return {
            'id': self.id,
            'pid': str(self.pid),
            'rig_type': self.resource_name,
            'watch': self.watching[:30],
            'trigger': self.trigger[:35],
            'status': self._status
        }

    def _setup_rig_logging(self):
        extra = {'rig_id': self.id}
        self.logger = logging.getLogger('rig')
        self.logger = logging.LoggerAdapter(self.logger, extra)
        self.console = logging.getLogger('rig_console')
        self.console = logging.LoggerAdapter(self.console, extra)
        self.log_debug("Initializing %s rig %s" %
                       (self.resource_name, self.id))

    def log_error(self, msg):
        self.logger.error(msg)
        if not self.detached:
            self.console.error(msg)

    def log_info(self, msg):
        self.logger.info(msg)
        if not self.detached:
            self.console.info(msg)

    def log_debug(self, msg):
        self.logger.debug(msg)
        if not self.detached and self.debug:
            self.console.debug(msg)

    def get_option(self, option):
        '''
        Retrieve a specified option from the loaded commandline options.

        An invalid option returns as False, rather than raises an exception.

        Returns
            str or bool - If the option has a value other than True, it is
                returned as a string. Otherwise return True or False depending
                on if it has a value at all.
        '''
        if option in self.args.keys():
            if not isinstance(self.args[option], bool):
                return str(self.args[option])
            else:
                return self.args[option]
        return False

    def set_parser_options(self, parser):
        '''
        This is where the rig-specific options are actually specified.

        Returns:
            parser - ArgumentParser (sub) parser object
        '''
        return parser

    def _fmt_return(self, command, output='', success=True):
        '''
        Formats a return value as a dict specifying the id of this rig, the
        command run, any output, and if the command was successful
        '''
        return json.dumps({
            'id': self.id,
            'command': command,
            'result': output,
            'success': success
        }).encode()

    def _listen_on_socket(self):
        self.log_debug('Listening on %s' % self._sock_address)
        while True:
            conn, client = self._sock.accept()
            try:
                req = json.loads(conn.recv(1024).decode())
            except Exception as err:
                self.log_debug("Error parsing socket request: %s" % err)
                return self._fmt_return(command="Unknown", result=err,
                                        success=False)
            self.log_debug("Received request '%s' from socket"
                           % req['command'])
            if req['command'] == 'destroy':
                self.log_debug("Shutting down rig")
                ret = self._fmt_return(command='destroy')
                conn.sendall(ret)
                raise DestroyRig
            try:
                ret = str(getattr(self, req['command'], extra=req['extra']))
                self.log_debug("Sending '%s' back to client" % ret)
                conn.sendall(self._fmt_return(command=req['command'],
                                              output=ret))
            except Exception as err:
                self.log_debug(err)
                self.log_error('No attribute: %s' % req['command'])
                conn.sendall(self._fmt_return(command=req['command'],
                                              output='No such attribute',
                                              success=False))
            continue

    def _register_actions(self):
        '''
        Compare the commandline options to supported actions for the rig.

        For any options matched against the supported actions, we initialize
        those actions to then be triggered once the rig hits the triggering
        conditions.
        '''
        self._actions = {}
        for action in self.supported_actions:
            _act = self.supported_actions[action]
            if action in self.args and self.args[_act.enabling_opt]:
                _action = self.supported_actions[action]
                loaded = _action.load(self.args)
                if not loaded:
                    self._exit(1)
                _action.set_tmp_dir(self._tmp_dir)
                self._actions[action] = _action

    def setup(self):
        '''
        MUST be overridden by rigs subclassing BaseRig.

        This is where rigs will define their watcher threads.
        '''
        raise NotImplementedError

    def execute(self):
        '''
        Main entry point for rigs.
        '''
        try:
            self._register_actions()
            self.setup()
            # detach from console
            if not self.foreground:
                print(self.id)
                self._detach()
                self.detached = True
                for action in self._actions:
                    self._actions[action].detached = True
            # listen on the UDS socket in one thread, spin the watcher
            # off in a separate thread
            _threads = []
            self._control_pool = ThreadPoolExecutor(2)
            _threads.append(self._control_pool.submit(self._listen_on_socket))
            _threads.append(self._control_pool.submit(self._monitor_resource))
            self._status = 'Running'
            ret = wait(_threads, return_when=FIRST_COMPLETED)
            self.report_created_files()
            self._cleanup()
            if ret:
                os._exit(0)
            else:
                os._exit(ret)
        except KeyboardInterrupt:
            self.log_debug('Received keyboard interrupt, destroying rig.')
            self._cleanup()
            self._exit(140)
        except Exception as err:
            self.log_error(err)
            self._cleanup()
            self._exit(1)

    def _monitor_resource(self):
        '''
        This is the main function in which we watch for a resource's trigger
        condition(s).

        This will block until the rig has self.triggered become True.
        '''
        try:
            ret = self.start_watcher_threads()
            if ret:
                self.log_info(
                    'Watcher thread triggered. Stopping other watcher threads')
                self.pool._threads.clear()
                self.trigger_actions()
        except Exception:
            raise

    def report_created_files(self):
        '''
        Report all files created by all actions
        '''
        if self.files:
            self.log_info("The following files were created for this rig: %s"
                          % ', '.join(f for f in self.files))

    def add_watcher_thread(self, target, args):
        '''
        Used by rigs to define new thread(s) to start in order to monitor their
        respective resources. Each required thread should make a separate call
        to add_watcher_thread().

        Positional Arguments:
            target - A callable method, almost always defined by the rig
            args - Args that should be passed to the target method, if multiple
                pass this as a tuple.
        '''
        if not callable(target):
            raise Exception("Unable to add watcher thread. Target must be a "
                            "callable method, received %s" % target.__class__)
        if not isinstance(args, tuple):
            args = (args, )
        self.watcher_threads.append((target, args))

    def start_watcher_threads(self):
        '''
        Start the threadpool and submits the requested watcher threads as jobs.

        Blocks until one of the threads returns, acting as a trigger event for
        the rig
        '''
        try:
            futures = []
            self.pool = ThreadPoolExecutor()
            for wthread in self.watcher_threads:
                futures.append(self.pool.submit(wthread[0], *wthread[1]))
            results = wait(futures, return_when=FIRST_COMPLETED)
            result = list(results[0])[0].result()
            return result
        except Exception as err:
            self.log_error("Exception caught for rig %s: %s" % (self.id, err))
            self._exit(1)

    def trigger_actions(self):
        '''
        This is called when a rig's monitoring condition is met. This will then
        invoke any and all actions defined by the user.
        '''
        self._status = 'Triggered'
        self.files = []
        try:
            for action in sorted(self._actions,
                                 key=lambda x: self._actions[x].priority):
                self.log_debug("Triggering action %s" % action)
                self._actions[action]._trigger_action()
                self.files.extend(self._actions[action].finish_execution())
        except Exception as err:
            self.log_error("Error executing actions: %s" % err)

    def _cleanup(self):
        try:
            self._status = 'Exiting'
            for action in self._actions:
                self._actions[action].cleanup()
            self.pool.shutdown(wait=False)
            self.pool._threads.clear()
            self._control_pool.shutdown(wait=False)
            self._control_pool._threads.clear()
            thread._threads_queues.clear()
        except Exception:
            pass
        try:
            os.remove(self._sock_address)
        except OSError as err:
            if err.errno == 2:
                pass
            else:
                self.log_error("Failed to remove listening socket %s: %s" %
                               (self._sock_address, err))
