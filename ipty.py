#!/usr/bin/env python

# ipty is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ipty is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with ipty. If not, see < http://www.gnu.org/licenses/ >.
#
# (C) 2014- by Adam Tauber, <asciimoo@gmail.com>

import os
import sys
import pty
import signal
import tty
import array
import termios
import fcntl
import select
import subprocess
from code import InteractiveConsole
from time import time
import math
import random

shell = 'sh'
if 'SHELL' in os.environ:
    shell = os.environ['SHELL']


class PTY(object):

    def __init__(self, command, input_modifiers=None, output_modifiers=None):
        self.input_modifiers = input_modifiers or []
        self.output_modifiers = output_modifiers or []
        self.user_input = ''
        self.master_fd = None
        self.cursor_offset = 0
        self.command = command

    def run(self):
        #self.reset_terminal()
        self._write_stdout('Started. Hit ^D or type "exit" to terminate\n')
        success = self._spawn()
        #self.reset_terminal()
        self._write_stdout('Finished\n')
        return success

    def reset_terminal(self):
        subprocess.call(["reset"])

    def _spawn(self):
        '''Create a spawned process.

        Based on pty.spawn() from standard library.
        '''

        assert self.master_fd is None

        pid, self.master_fd = pty.fork()

        if pid == pty.CHILD:
            os.execlp(self.command[0], *self.command)

        old_handler = signal.signal(signal.SIGWINCH, self._signal_winch)

        try:
            mode = tty.tcgetattr(pty.STDIN_FILENO)
            tty.setraw(pty.STDIN_FILENO)
            restore = 1
        except tty.error: # This is the same as termios.error
            restore = 0

        self._set_pty_size()

        try:
            self._copy()
        except (IOError, OSError):
            if restore:
                tty.tcsetattr(pty.STDIN_FILENO, tty.TCSAFLUSH, mode)

        os.close(self.master_fd)
        self.master_fd = None
        signal.signal(signal.SIGWINCH, old_handler)

        return True

    def _signal_winch(self, signal, frame):
        '''Signal handler for SIGWINCH - window size has changed.'''

        self._set_pty_size()

    def _set_pty_size(self):
        '''
        Sets the window size of the child pty based on the window size
        of our own controlling terminal.
        '''

        assert self.master_fd is not None

        # Get the terminal size of the real terminal, set it on the pseudoterminal.
        buf = array.array('h', [0, 0, 0, 0])
        fcntl.ioctl(pty.STDOUT_FILENO, termios.TIOCGWINSZ, buf, True)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, buf)

    def _copy(self):
        '''Main select loop.

        Passes control to self._master_read() or self._stdin_read()
        when new data arrives.
        '''

        assert self.master_fd is not None

        while 1:
            try:
                rfds, wfds, xfds = select.select([self.master_fd, pty.STDIN_FILENO], [], [])
            except select.error, e:
                if e[0] == 4:   # Interrupted system call.
                    continue

            if self.master_fd in rfds:
                data = os.read(self.master_fd, 1024)

                if len(data) == 0:
                  break

                self._handle_master_read(data)

            if pty.STDIN_FILENO in rfds:
                data = os.read(pty.STDIN_FILENO, 1024)
                self._handle_stdin_read(data)

    def _handle_master_read(self, data):
        '''Handles new data on child process stdout.'''

        self._write_stdout(data)

    def _handle_stdin_read(self, data):
        '''Handles new data on child process stdin.'''
        # TODO better stdin handling
        write_out = True
        #self._write_stdout(repr(data))
        # enter, ctrl+c
        if data in ('\r', '\x03'):
            #self._write_stdout(self.user_input)
            self.user_input = ''
            self.cursor_offset = 0
        # arrows, pageup, pagedown
        elif data.startswith('\x1b'):
            # ('\x1bOA', '\x1bOB', '\x1bOC', '\x1bOD') up down left right
            if data == '\x1bOC':
                self.cursor_offset -= 1
            elif data == '\x1bOD':
                self.cursor_offset += 1
            else:
                self.user_input = ''
        # backspace
        elif data in ('\x08', '\x7f'):
            self.user_input = self.user_input[:-1]
        else:
            if self.cursor_offset == 0:
                self.user_input += data
            else:
                self.user_input = self.user_input[:self.cursor_offset]+data+self.user_input[self.cursor_offset:]
            for m in self.input_modifiers:
                if not m(data):
                    write_out = False
        if write_out:
            self._write_master(data)

    def _write_stdout(self, data):
        '''Writes to stdout as if the child process had written the data.'''

        os.write(pty.STDOUT_FILENO, data)

    def _write_master(self, data):
        '''Writes to the child process from its controlling terminal.'''

        assert self.master_fd is not None

        while data != '':
            n = os.write(self.master_fd, data)
            data = data[n:]


class InputFilter():
    def __init__(self, regex, pty):
        self.regex = regex
        self.pty = pty

    def __call__(self, data):
        match = self.regex.search(self.pty.user_input)
        if match:
            match_len = len(match.groups()[0])
            self.pty._write_master('\x08'*(match_len - 1))
            self.pty.user_input = self.pty.user_input[:-match_len]
            return False
        return True


class FileCacher:
    "Cache the stdout text so we can analyze it before returning it"
    def __init__(self):
        self.reset()

    def reset(self):
        self.out = []

    def write(self, line):
        self.out.append(str(line))

    def flush(self):
        output = '\n'.join(self.out)
        self.reset()
        return output


class Shell(InteractiveConsole):
    "Wrapper around Python that can filter input/output to the shell"
    def __init__(self):
        self.stdout = sys.stdout
        self.cache = FileCacher()
        libs = globals()
        libs['time'] = time
        libs['math'] = math
        libs['random'] = random
        InteractiveConsole.__init__(self, libs)
        return

    def get_output(self):
        sys.stdout = self.cache

    def return_output(self):
        sys.stdout = self.stdout

    def push(self, line):
        self.get_output()
        # you can filter input here by doing something like
        # line = filter(line)
        InteractiveConsole.push(self, line)
        self.return_output()
        output = self.cache.flush()
        # you can filter the output here by doing something like
        # output = filter(output)
        return output  # or do something else with it


class InputEval():
    def __init__(self, pty):
        self.pty = pty
        self.regex = re.compile('~~([^~]+)~~')
        self.console = Shell()

    def __call__(self, data):
        match = self.regex.search(self.pty.user_input)
        if match:
            match_str = match.groups()[0]
            match_len = len(match_str)
            self.pty._write_master('\x08'*(match_len + 3))
            self.pty.user_input = self.pty.user_input[:-(match_len + 4)]
            ret = self.console.push(match_str.strip()).strip()
            self.pty.user_input += ret
            self.pty._write_master(ret)
            return False
        return True


class InputCompletition():
    def __init__(self, terms, pty, min_match_length=4):
        self.pty = pty
        self.terms = terms
        self.min_match_length = min_match_length

    def __call__(self, data):
        if not self.pty.user_input.strip():
            return True
        if len(self.pty.user_input) < self.min_match_length:
            return True
        completition = ''
        for term in self.terms:
            cut = len(term) - self.min_match_length
            while cut > 0:
                valid_pos = len(self.pty.user_input) - len(term[:-cut])
                if valid_pos < 0:
                    break
                if self.pty.user_input.find(term[:-cut]) == valid_pos:
                    if completition:
                        return True
                    completition = term[-cut:]
                    break
                cut -= 1
        if completition:
            self.pty._write_master(data + completition)
            self.pty.user_input += data + completition
            return False
        return True


if __name__ == '__main__':
    import re
    p = PTY([shell])
    p.input_modifiers.append(InputFilter(re.compile('.*(?:^|\W)(nsa$)', re.I), p))
    p.input_modifiers.append(InputCompletition(('-af volume=',), p))
    p.input_modifiers.append(InputEval(p))
    p.run()
