# Copyright (C) 2001-2006 by the Free Software Foundation, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

import os
import grp
import pwd
import sys
import errno
import signal
import socket
import logging
import optparse

from Mailman import Defaults
from Mailman import Errors
from Mailman import LockFile
from Mailman import Utils
from Mailman import Version
from Mailman import loginit
from Mailman.MailList import MailList
from Mailman.configuration import config
from Mailman.i18n import _

__i18n_templates__ = True

COMMASPACE = ', '
DOT = '.'

# Since we wake up once per day and refresh the lock, the LOCK_LIFETIME
# needn't be (much) longer than SNOOZE.  We pad it 6 hours just to be safe.
LOCK_LIFETIME = Defaults.days(1) + Defaults.hours(6)
SNOOZE = Defaults.days(1)
MAX_RESTARTS = 10



def parseargs():
    parser = optparse.OptionParser(version=Version.MAILMAN_VERSION,
                                   usage=_("""\
Primary start-up and shutdown script for Mailman's qrunner daemon.

This script starts, stops, and restarts the main Mailman queue runners, making
sure that the various long-running qrunners are still alive and kicking.  It
does this by forking and exec'ing the qrunners and waiting on their pids.
When it detects a subprocess has exited, it may restart it.

The qrunners respond to SIGINT, SIGTERM, and SIGHUP.  SIGINT and SIGTERM both
cause the qrunners to exit cleanly, but the master will only restart qrunners
that have exited due to a SIGINT.  SIGHUP causes the master and the qrunners
to close their log files, and reopen then upon the next printed message.

The master also responds to SIGINT, SIGTERM, and SIGHUP, which it simply
passes on to the qrunners (note that the master will close and reopen its own
log files on receipt of a SIGHUP).  The master also leaves its own process id
in the file data/master-qrunner.pid but you normally don't need to use this
pid directly.  The `start', `stop', `restart', and `reopen' commands handle
everything for you.

Commands:

    start   - Start the master daemon and all qrunners.  Prints a message and
              exits if the master daemon is already running.

    stop    - Stops the master daemon and all qrunners.  After stopping, no
              more messages will be processed.

    restart - Restarts the qrunners, but not the master process.  Use this
              whenever you upgrade or update Mailman so that the qrunners will
              use the newly installed code.

    reopen  - This will close all log files, causing them to be re-opened the
              next time a message is written to them

Usage: %prog [options] [ start | stop | restart | reopen ]"""))
    parser.add_option('-n', '--no-restart',
                      dest='restart', default=True, action='store_false',
                      help=_("""\
Don't restart the qrunners when they exit because of an error or a SIGINT.
They are never restarted if they exit in response to a SIGTERM.  Use this only
for debugging.  Only useful if the `start' command is given."""))
    parser.add_option('-u', '--run-as-user',
                      dest='checkprivs', default=True, action='store_false',
                      help=_("""\
Normally, this script will refuse to run if the user id and group id are not
set to the `mailman' user and group (as defined when you configured Mailman).
If run as root, this script will change to this user and group before the
check is made.

This can be inconvenient for testing and debugging purposes, so the -u flag
means that the step that sets and checks the uid/gid is skipped, and the
program is run as the current user and group.  This flag is not recommended
for normal production environments.

Note though, that if you run with -u and are not in the mailman group, you may
have permission problems, such as begin unable to delete a list's archives
through the web.  Tough luck!"""))
    parser.add_option('-s', '--stale-lock-cleanup',
                      dest='force', default=False, action='store_true',
                      help=_("""\
If mailmanctl finds an existing master lock, it will normally exit with an
error message.  With this option, mailmanctl will perform an extra level of
checking.  If a process matching the host/pid described in the lock file is
running, mailmanctl will still exit, but if no matching process is found,
mailmanctl will remove the apparently stale lock and make another attempt to
claim the master lock."""))
    parser.add_option('-q', '--quiet',
                      default=False, action='store_true',
                      help=_("""\
Don't print status messages.  Error messages are still printed to standard
error."""))
    parser.add_option('-C', '--config',
                      help=_('Alternative configuration file to use'))
    opts, args = parser.parse_args()
    if not args:
        parser.print_help()
        print >> sys.stderr, _('No command given.')
        sys.exit(1)
    if len(args) > 1:
        parse.print_help()
        commands = COMMASPACE.join(args)
        print >> sys.stderr, _('Bad command: $commands')
        sys.exit(1)
    return parser, opts, args



def kill_watcher(sig):
    try:
        fp = open(config.PIDFILE)
        pidstr = fp.read()
        fp.close()
        pid = int(pidstr.strip())
    except (IOError, ValueError), e:
        # For i18n convenience
        pidfile = config.PIDFILE
        print >> sys.stderr, _('PID unreadable in: $pidfile')
        print >> sys.stderr, e
        print >> sys.stderr, _('Is qrunner even running?')
        return
    try:
        os.kill(pid, sig)
    except OSError, e:
        if e.errno <> errno.ESRCH: raise
        print >> sys.stderr, _('No child with pid: $pid')
        print >> sys.stderr, e
        print >> sys.stderr, _('Stale pid file removed.')
        os.unlink(config.PIDFILE)



def get_lock_data():
    # Return the hostname, pid, and tempfile
    fp = open(config.LOCK_FILE)
    try:
        filename = os.path.split(fp.read().strip())[1]
    finally:
        fp.close()
    parts = filename.split('.')
    hostname = DOT.join(parts[1:-1])
    pid = int(parts[-1])
    return hostname, int(pid), filename


def qrunner_state():
    # 1 if proc exists on host (but is it qrunner? ;)
    # 0 if host matches but no proc
    # hostname if hostname doesn't match
    hostname, pid, tempfile = get_lock_data()
    if hostname <> socket.gethostname():
        return hostname
    # Find out if the process exists by calling kill with a signal 0.
    try:
        os.kill(pid, 0)
    except OSError, e:
        if e.errno <> errno.ESRCH:
            raise
        return 0
    return 1


def acquire_lock_1(force):
    # Be sure we can acquire the master qrunner lock.  If not, it means some
    # other master qrunner daemon is already going.
    lock = LockFile.LockFile(config.LOCK_FILE, LOCK_LIFETIME)
    try:
        lock.lock(0.1)
        return lock
    except LockFile.TimeOutError:
        if not force:
            raise
        # Force removal of lock first
        lock._disown()
        hostname, pid, tempfile = get_lock_data()
        os.unlink(config.LOCK_FILE)
        os.unlink(os.path.join(config.LOCK_DIR, tempfile))
        return acquire_lock_1(force=False)


def acquire_lock(force):
    try:
        lock = acquire_lock_1(force)
        return lock
    except LockFile.TimeOutError:
        status = qrunner_state()
        if status == 1:
            # host matches and proc exists
            print >> sys.stderr, _("""\
The master qrunner lock could not be acquired because it appears as if another
master qrunner is already running.
""")
        elif status == 0:
            # host matches but no proc
            print >> sys.stderr, _("""\
The master qrunner lock could not be acquired.  It appears as though there is
a stale master qrunner lock.  Try re-running mailmanctl with the -s flag.
""")
        else:
            # host doesn't even match
            print >> sys.stderr, _("""\
The master qrunner lock could not be acquired, because it appears as if some
process on some other host may have acquired it.  We can't test for stale
locks across host boundaries, so you'll have to do this manually.  Or, if you
know the lock is stale, re-run mailmanctl with the -s flag.

Lock file: $config.LOCK_FILE
Lock host: $status

Exiting.""")



def start_runner(qrname, slice, count):
    pid = os.fork()
    if pid:
        # parent
        return pid
    # child
    #
    # Craft the command line arguments for the exec() call.
    rswitch = '--runner=%s:%d:%d' % (qrname, slice, count)
    exe = os.path.join(config.BIN_DIR, 'qrunner')
    # config.PYTHON, which is the absolute path to the Python interpreter,
    # must be given as argv[0] due to Python's library search algorithm.
    args = [config.PYTHON, config.PYTHON, exe, rswitch, '-s']
    if opts.config:
        args.extend(['-C', opts.config])
    os.execl(*args)
    # Should never get here
    raise RuntimeError, 'os.execl() failed'


def start_all_runners():
    kids = {}
    for qrname, count in config.QRUNNERS:
        for slice in range(count):
            # queue runner name, slice, numslices, restart count
            info = (qrname, slice, count, 0)
            pid = start_runner(qrname, slice, count)
            kids[pid] = info
    return kids



def check_privs():
    # If we're running as root (uid == 0), coerce the uid and gid to that
    # which Mailman was configured for, and refuse to run if we didn't coerce
    # the uid/gid.
    gid = grp.getgrnam(config.MAILMAN_GROUP)[2]
    uid = pwd.getpwnam(config.MAILMAN_USER)[2]
    myuid = os.getuid()
    if myuid == 0:
        # Set the process's supplimental groups.
        groups = [x[2] for x in grp.getgrall() if config.MAILMAN_USER in x[3]]
        groups.append(gid)
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
    elif myuid <> uid:
        name = config.MAILMAN_USER
        usage(1, _(
            'Run this program as root or as the $name user, or use -u.'))



def main():
    global elog, qlog, opts

    parser, opts, args = parseargs()
    config.load(opts.config)

    loginit.initialize()
    elog = logging.getLogger('mailman.error')
    qlog = logging.getLogger('mailman.qrunner')

    if opts.checkprivs:
        check_privs()
    else:
        print _('Warning!  You may encounter permission problems.')

    # Handle the commands
    command = args[0].lower()
    if command == 'stop':
        # Sent the master qrunner process a SIGINT, which is equivalent to
        # giving cron/qrunner a ctrl-c or KeyboardInterrupt.  This will
        # effectively shut everything down.
        if not opts.quiet:
            print _("Shutting down Mailman's master qrunner")
        kill_watcher(signal.SIGTERM)
    elif command == 'restart':
        # Sent the master qrunner process a SIGHUP.  This will cause the
        # master qrunner to kill and restart all the worker qrunners, and to
        # close and re-open its log files.
        if not opts.quiet:
            print _("Restarting Mailman's master qrunner")
        kill_watcher(signal.SIGINT)
    elif command == 'reopen':
        if not opts.quiet:
            print _('Re-opening all log files')
        kill_watcher(signal.SIGHUP)
    elif command == 'start':
        # Here's the scoop on the processes we're about to create.  We'll need
        # one for each qrunner, and one for a master child process watcher /
        # lock refresher process.
        #
        # The child watcher process simply waits on the pids of the children
        # qrunners.  Unless explicitly disabled by a mailmanctl switch (or the
        # children are killed with SIGTERM instead of SIGINT), the watcher
        # will automatically restart any child process that exits.  This
        # allows us to be more robust, and also to implement restart by simply
        # SIGINT'ing the qrunner children, and letting the watcher restart
        # them.
        #
        # Under normal operation, we have a child per queue.  This lets us get
        # the most out of the available resources, since a qrunner with no
        # files in its queue directory is pretty cheap, but having a separate
        # runner process per queue allows for a very responsive system.  Some
        # people want a more traditional (i.e. MM2.0.x) cron-invoked qrunner.
        # No problem, but using mailmanctl isn't the answer.  So while
        # mailmanctl hard codes some things, others, such as the number of
        # qrunners per queue, are configurable.
        #
        # First, acquire the master mailmanctl lock
        lock = acquire_lock(opts.force)
        if not lock:
            return
        # Daemon process startup according to Stevens, Advanced Programming in
        # the UNIX Environment, Chapter 13.
        pid = os.fork()
        if pid:
            # parent
            if not opts.quiet:
                print _("Starting Mailman's master qrunner.")
            # Give up the lock "ownership".  This just means the foreground
            # process won't close/unlock the lock when it finalizes this lock
            # instance.  We'll let the mater watcher subproc own the lock.
            lock._transfer_to(pid)
            return
        # child
        lock._take_possession()
        # First, save our pid in a file for "mailmanctl stop" rendezvous.  We
        # want the perms on the .pid file to be rw-rw----
        omask = os.umask(6)
        try:
            fp = open(config.PIDFILE, 'w')
            print >> fp, os.getpid()
            fp.close()
        finally:
            os.umask(omask)
        # Create a new session and become the session leader, but since we
        # won't be opening any terminal devices, don't do the ultra-paranoid
        # suggestion of doing a second fork after the setsid() call.
        os.setsid()
        # Instead of cd'ing to root, cd to the Mailman installation home
        os.chdir(config.PREFIX)
        # Set our file mode creation umask
        os.umask(007)
        # I don't think we have any unneeded file descriptors.
        #
        # Now start all the qrunners.  This returns a dictionary where the
        # keys are qrunner pids and the values are tuples of the following
        # form: (qrname, slice, count).  This does its own fork and exec, and
        # sets up its own signal handlers.
        kids = start_all_runners()
        # Set up a SIGALRM handler to refresh the lock once per day.  The lock
        # lifetime is 1day+6hours so this should be plenty.
        def sigalrm_handler(signum, frame):
            lock.refresh()
            signal.alarm(Defaults.days(1))
        signal.signal(signal.SIGALRM, sigalrm_handler)
        signal.alarm(Defaults.days(1))
        # Set up a SIGHUP handler so that if we get one, we'll pass it along
        # to all the qrunner children.  This will tell them to close and
        # reopen their log files
        def sighup_handler(signum, frame):
            loginit.reopen()
            for pid in kids.keys():
                os.kill(pid, signal.SIGHUP)
            # And just to tweak things...
            qlog.info('Master watcher caught SIGHUP.  Re-opening log files.')
        signal.signal(signal.SIGHUP, sighup_handler)
        # We also need to install a SIGTERM handler because that's what init
        # will kill this process with when changing run levels.
        def sigterm_handler(signum, frame):
            for pid in kids.keys():
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError, e:
                    if e.errno <> errno.ESRCH: raise
            qlog.info('Master watcher caught SIGTERM.  Exiting.')
        signal.signal(signal.SIGTERM, sigterm_handler)
        # Finally, we need a SIGINT handler which will cause the sub-qrunners
        # to exit, but the master will restart SIGINT'd sub-processes unless
        # the -n flag was given.
        def sigint_handler(signum, frame):
            for pid in kids.keys():
                os.kill(pid, signal.SIGINT)
            qlog.info('Master watcher caught SIGINT.  Restarting.')
        signal.signal(signal.SIGINT, sigint_handler)
        # Now we're ready to simply do our wait/restart loop.  This is the
        # master qrunner watcher.
        try:
            while True:
                try:
                    pid, status = os.wait()
                except OSError, e:
                    # No children?  We're done
                    if e.errno == errno.ECHILD:
                        break
                    # If the system call got interrupted, just restart it.
                    elif e.errno <> errno.EINTR:
                        raise
                    continue
                killsig = exitstatus = None
                if os.WIFSIGNALED(status):
                    killsig = os.WTERMSIG(status)
                if os.WIFEXITED(status):
                    exitstatus = os.WEXITSTATUS(status)
                # We'll restart the process unless we were given the
                # "no-restart" switch, or if the process was SIGTERM'd or
                # exitted with a SIGTERM exit status.  This lets us better
                # handle runaway restarts (say, if the subproc had a syntax
                # error!)
                restarting = ''
                if opts.restart:
                    if ((exitstatus == None and killsig <> signal.SIGTERM) or
                        (killsig == None and exitstatus <> signal.SIGTERM)):
                        # Then
                        restarting = '[restarting]'
                qrname, slice, count, restarts = kids[pid]
                del kids[pid]
                qlog.info("""\
Master qrunner detected subprocess exit
(pid: %d, sig: %s, sts: %s, class: %s, slice: %d/%d) %s""",
                       pid, killsig, exitstatus, qrname,
                       slice+1, count, restarting)
                # See if we've reached the maximum number of allowable restarts
                if exitstatus <> signal.SIGINT:
                    restarts += 1
                if restarts > MAX_RESTARTS:
                    qlog.info("""\
Qrunner %s reached maximum restart limit of %d, not restarting.""",
                           qrname, MAX_RESTARTS)
                    restarting = ''
                # Now perhaps restart the process unless it exited with a
                # SIGTERM or we aren't restarting.
                if restarting:
                    newpid = start_runner(qrname, slice, count)
                    kids[newpid] = (qrname, slice, count, restarts)
        finally:
            # Should we leave the main loop for any reason, we want to be sure
            # all of our children are exited cleanly.  Send SIGTERMs to all
            # the child processes and wait for them all to exit.
            for pid in kids.keys():
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError, e:
                    if e.errno == errno.ESRCH:
                        # The child has already exited
                        qlog.info('ESRCH on pid: %d', pid)
                        del kids[pid]
            # Wait for all the children to go away
            while True:
                try:
                    pid, status = os.wait()
                except OSError, e:
                    if e.errno == errno.ECHILD:
                        break
                    elif e.errno <> errno.EINTR:
                        raise
                    continue
        # Finally, give up the lock
        lock.unlock(unconditionally=True)
        os._exit(0)



if __name__ == '__main__':
    main()