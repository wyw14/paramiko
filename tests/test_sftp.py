# Copyright (C) 2003-2009  Robey Pointer <robeypointer@gmail.com>
#
# This file is part of paramiko.
#
# Paramiko is free software; you can redistribute it and/or modify it under the
# terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# Paramiko is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Paramiko; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA.

"""
some unit tests to make sure sftp works.

a real actual sftp server is contacted, and a new folder is created there to
do test file operations in (so no existing files will be harmed).
"""

import gc
import os
import socket
import sys
import threading
import warnings
import weakref
from binascii import hexlify
from collections import deque
from io import StringIO
from tempfile import mkstemp

import pytest

from paramiko.common import o777, o600, o666, o644
from paramiko.message import Message
from paramiko.sftp import (
    BaseSFTP,
    CMD_CLOSE,
    CMD_HANDLE,
    CMD_NAME,
    CMD_OPEN,
    CMD_OPENDIR,
    CMD_READDIR,
    CMD_STATUS,
    SFTP_EOF,
    SFTP_FAILURE,
    SFTP_OK,
)
from paramiko.sftp_attr import SFTPAttributes
from paramiko.sftp_client import SFTPClient, _SFTPDirectoryIterator
from paramiko.util import b, u
from tests import requireNonAsciiLocale

from ._util import needs_builtin
from ._util import slow


ARTICLE = """
Insulin sensitivity and liver insulin receptor structure in ducks from two
genera

T. Constantine, B. Chevalier, M. Derouet and J. Simon
Station de Recherches Avicoles, Institut National de la Recherche Agronomique,
Nouzilly, France.

Insulin sensitivity and liver insulin receptor structure were studied in
5-wk-old ducks from two genera (Muscovy and Pekin). In the fasting state, both
duck types were equally resistant to exogenous insulin compared with chicken.
Despite the low potency of duck insulin, the number of insulin receptors was
lower in Muscovy duck and similar in Pekin duck and chicken liver membranes.
After 125I-insulin cross-linking, the size of the alpha-subunit of the
receptors from the three species was 135,000. Wheat germ agglutinin-purified
receptors from the three species were contaminated by an active and unusual
adenosinetriphosphatase (ATPase) contaminant (highest activity in Muscovy
duck). Sequential purification of solubilized receptor from both duck types on
lentil and then wheat germ agglutinin lectins led to a fraction of receptors
very poor in ATPase activity that exhibited a beta-subunit size (95,000) and
tyrosine kinase activity similar to those of ATPase-free chicken insulin
receptors. Therefore the ducks from the two genera exhibit an alpha-beta-
structure for liver insulin receptors and a clear difference in the number of
liver insulin receptors. Their sensitivity to insulin is, however, similarly
decreased compared with chicken.
"""


# Here is how unicode characters are encoded over 1 to 6 bytes in utf-8
# U-00000000 - U-0000007F:
#   0xxxxxxx
# U-00000080 - U-000007FF:
#   110xxxxx 10xxxxxx
# U-00000800 - U-0000FFFF:
#   1110xxxx 10xxxxxx 10xxxxxx
# U-00010000 - U-001FFFFF:
#   11110xxx 10xxxxxx 10xxxxxx 10xxxxxx
# U-00200000 - U-03FFFFFF:
#   111110xx 10xxxxxx 10xxxxxx 10xxxxxx 10xxxxxx
# U-04000000 - U-7FFFFFFF:
#   1111110x 10xxxxxx 10xxxxxx 10xxxxxx 10xxxxxx 10xxxxxx
# Note that: hex(int('11000011',2)) == '0xc3'
# Thus, the following 2-bytes sequence is not valid utf8: "invalid continuation
# byte"
NON_UTF8_DATA = b"\xC3\xC3"

unicode_folder = "\u00fcnic\u00f8de"
utf8_folder = b"/\xc3\xbcnic\xc3\xb8\x64\x65"


@slow
class TestSFTP:
    def test_file(self, sftp):
        """
        verify that we can create a file.
        """
        f = sftp.open(sftp.FOLDER + "/test", "w")
        try:
            assert f.stat().st_size == 0
        finally:
            f.close()
            sftp.remove(sftp.FOLDER + "/test")

    def test_close(self, sftp):
        """
        Verify that SFTP session close() causes a socket error on next action.
        """
        sftp.close()
        with pytest.raises(socket.error, match="Socket is closed"):
            sftp.open(sftp.FOLDER + "/test2", "w")

    def test_sftp_can_be_used_as_context_manager(self, sftp):
        """
        verify that the sftp session is closed when exiting the context manager
        """
        with sftp:
            pass
        with pytest.raises(socket.error, match="Socket is closed"):
            sftp.open(sftp.FOLDER + "/test2", "w")

    def test_write(self, sftp):
        """
        verify that a file can be created and written, and the size is correct.
        """
        try:
            with sftp.open(sftp.FOLDER + "/duck.txt", "w") as f:
                f.write(ARTICLE)
            assert sftp.stat(sftp.FOLDER + "/duck.txt").st_size == 1486
        finally:
            sftp.remove(sftp.FOLDER + "/duck.txt")

    def test_sftp_file_can_be_used_as_context_manager(self, sftp):
        """
        verify that an opened file can be used as a context manager
        """
        try:
            with sftp.open(sftp.FOLDER + "/duck.txt", "w") as f:
                f.write(ARTICLE)
            assert sftp.stat(sftp.FOLDER + "/duck.txt").st_size == 1486
        finally:
            sftp.remove(sftp.FOLDER + "/duck.txt")

    def test_append(self, sftp):
        """
        verify that a file can be opened for append, and tell() still works.
        """
        try:
            with sftp.open(sftp.FOLDER + "/append.txt", "w") as f:
                f.write("first line\nsecond line\n")
                assert f.tell() == 23

            with sftp.open(sftp.FOLDER + "/append.txt", "a+") as f:
                f.write("third line!!!\n")
                assert f.tell() == 37
                assert f.stat().st_size == 37
                f.seek(-26, f.SEEK_CUR)
                assert f.readline() == "second line\n"
        finally:
            sftp.remove(sftp.FOLDER + "/append.txt")

    def test_rename(self, sftp):
        """
        verify that renaming a file works.
        """
        try:
            with sftp.open(sftp.FOLDER + "/first.txt", "w") as f:
                f.write("content!\n")
            sftp.rename(
                sftp.FOLDER + "/first.txt", sftp.FOLDER + "/second.txt"
            )
            with pytest.raises(IOError, match="No such file"):
                sftp.open(sftp.FOLDER + "/first.txt", "r")
            with sftp.open(sftp.FOLDER + "/second.txt", "r") as f:
                f.seek(-6, f.SEEK_END)
                assert u(f.read(4)) == "tent"
        finally:
            # TODO: this is gross, make some sort of 'remove if possible' / 'rm
            # -f' a-like, jeez
            try:
                sftp.remove(sftp.FOLDER + "/first.txt")
            except:
                pass
            try:
                sftp.remove(sftp.FOLDER + "/second.txt")
            except:
                pass

    def testa_posix_rename(self, sftp):
        """Test posix-rename@openssh.com protocol extension."""
        try:
            # first check that the normal rename works as specified
            with sftp.open(sftp.FOLDER + "/a", "w") as f:
                f.write("one")
            sftp.rename(sftp.FOLDER + "/a", sftp.FOLDER + "/b")
            with sftp.open(sftp.FOLDER + "/a", "w") as f:
                f.write("two")
            with pytest.raises(IOError):  # actual message seems generic
                sftp.rename(sftp.FOLDER + "/a", sftp.FOLDER + "/b")

            # now check with the posix_rename
            sftp.posix_rename(sftp.FOLDER + "/a", sftp.FOLDER + "/b")
            with sftp.open(sftp.FOLDER + "/b", "r") as f:
                data = u(f.read())
            err = "Contents of renamed file not the same as original file"
            assert "two" == data, err

        finally:
            try:
                sftp.remove(sftp.FOLDER + "/a")
            except:
                pass
            try:
                sftp.remove(sftp.FOLDER + "/b")
            except:
                pass

    def test_folder(self, sftp):
        """
        create a temporary folder, verify that we can create a file in it, then
        remove the folder and verify that we can't create a file in it anymore.
        """
        sftp.mkdir(sftp.FOLDER + "/subfolder")
        sftp.open(sftp.FOLDER + "/subfolder/test", "w").close()
        sftp.remove(sftp.FOLDER + "/subfolder/test")
        sftp.rmdir(sftp.FOLDER + "/subfolder")
        # shouldn't be able to create that file if dir removed
        with pytest.raises(IOError, match="No such file"):
            sftp.open(sftp.FOLDER + "/subfolder/test")

    def test_listdir(self, sftp):
        """
        verify that a folder can be created, a bunch of files can be placed in
        it, and those files show up in sftp.listdir.
        """
        try:
            sftp.open(sftp.FOLDER + "/duck.txt", "w").close()
            sftp.open(sftp.FOLDER + "/fish.txt", "w").close()
            sftp.open(sftp.FOLDER + "/tertiary.py", "w").close()

            x = sftp.listdir(sftp.FOLDER)
            assert len(x) == 3
            assert "duck.txt" in x
            assert "fish.txt" in x
            assert "tertiary.py" in x
            assert "random" not in x
        finally:
            sftp.remove(sftp.FOLDER + "/duck.txt")
            sftp.remove(sftp.FOLDER + "/fish.txt")
            sftp.remove(sftp.FOLDER + "/tertiary.py")

    def test_listdir_iter(self, sftp):
        """
        listdir_iter version of above test
        """
        try:
            sftp.open(sftp.FOLDER + "/duck.txt", "w").close()
            sftp.open(sftp.FOLDER + "/fish.txt", "w").close()
            sftp.open(sftp.FOLDER + "/tertiary.py", "w").close()

            x = [x.filename for x in sftp.listdir_iter(sftp.FOLDER)]
            assert len(x) == 3
            assert "duck.txt" in x
            assert "fish.txt" in x
            assert "tertiary.py" in x
            assert "random" not in x
        finally:
            sftp.remove(sftp.FOLDER + "/duck.txt")
            sftp.remove(sftp.FOLDER + "/fish.txt")
            sftp.remove(sftp.FOLDER + "/tertiary.py")

    def _populate_listdir(self, sftp, count):
        for i in range(count):
            sftp.open("{}/file{:02d}".format(sftp.FOLDER, i), "w").close()

    def _cleanup_listdir(self, sftp, count):
        for i in range(count):
            sftp.remove("{}/file{:02d}".format(sftp.FOLDER, i))

    def _command_spy(self, sftp):
        seen = []
        original = sftp._async_request

        def spy(fileobj, t, *args):
            seen.append(t)
            return original(fileobj, t, *args)

        sftp._async_request = spy
        return seen

    def test_listdir_iter_closes_handle_on_break(self, sftp):
        """
        stopping a listdir_iter early (then closing it) sends exactly one
        close and leaves the client usable for file opens and more listings
        """
        try:
            self._populate_listdir(sftp, 3)
            seen = self._command_spy(sftp)
            iterator = sftp.listdir_iter(sftp.FOLDER)
            next(iterator)
            iterator.close()
            assert seen.count(CMD_CLOSE) == 1
            # idempotent: no second close packet for a dead handle
            iterator.close()
            assert seen.count(CMD_CLOSE) == 1

            # the same client must still work: open a file ...
            with sftp.open(sftp.FOLDER + "/duck.txt", "w") as f:
                f.write(b"quack")
            with sftp.open(sftp.FOLDER + "/duck.txt", "r") as f:
                assert f.read() == b"quack"

            # ... and list the directory again, fully, with one new
            # directory-handle close (the two file closes above also show
            # up in the spy, so compare against the current count)
            closes_before = seen.count(CMD_CLOSE)
            again = [
                x.filename for x in sftp.listdir_iter(sftp.FOLDER, 1)
            ]
            assert sorted(again) == sorted(
                ["file00", "file01", "file02", "duck.txt"]
            )
            assert seen.count(CMD_CLOSE) == closes_before + 1
            sftp.remove(sftp.FOLDER + "/duck.txt")
        finally:
            self._cleanup_listdir(sftp, 3)

    def test_listdir_iter_closes_handle_on_exception(self, sftp):
        """
        an exception raised while iterating a directory used as a context
        manager still reaps the remote handle exactly once
        """
        try:
            self._populate_listdir(sftp, 3)
            seen = self._command_spy(sftp)
            with pytest.raises(RuntimeError):
                with sftp.listdir_iter(sftp.FOLDER) as iterator:
                    next(iterator)
                    raise RuntimeError("boom")
            assert seen.count(CMD_CLOSE) == 1
            # client survives and lists normally afterwards
            assert len(list(sftp.listdir_iter(sftp.FOLDER))) == 3
            assert seen.count(CMD_CLOSE) == 2
        finally:
            self._cleanup_listdir(sftp, 3)

    def test_listdir_iter_closes_handle_on_gc(self, sftp):
        """
        an abandoned, un-closed iterator closes its remote handle once it is
        garbage collected, without waiting for any remote timeout
        """
        try:
            self._populate_listdir(sftp, 3)
            seen = self._command_spy(sftp)

            def abandon():
                iterator = sftp.listdir_iter(sftp.FOLDER)
                next(iterator)
                # function returns without closing the iterator

            abandon()
            gc.collect()
            assert seen.count(CMD_CLOSE) == 1
            gc.collect()
            assert seen.count(CMD_CLOSE) == 1

            # no stale readdir responses are left in the stream: the client
            # keeps working normally
            assert sorted(
                x.filename for x in sftp.listdir_iter(sftp.FOLDER)
            ) == ["file00", "file01", "file02"]
        finally:
            self._cleanup_listdir(sftp, 3)

    def test_listdir_iter_client_close_reaps_handle(self, sftp):
        """
        closing the SFTPClient reaps a directory handle left open by an
        iterator that was only partially consumed
        """
        # the test folder is removed wholesale by the fixture teardown;
        # the client itself is closed in this test
        self._populate_listdir(sftp, 3)
        seen = self._command_spy(sftp)
        iterator = sftp.listdir_iter(sftp.FOLDER)
        next(iterator)
        sftp.close()
        assert seen.count(CMD_CLOSE) == 1
        # a closed iterator stops cleanly instead of touching the dead
        # channel (which would surface as a network error)
        with pytest.raises(StopIteration):
            next(iterator)

    def test_listdir_iter_paginated_entries_once(self, sftp):
        """
        entries spread over several server pages are delivered exactly once
        and the handle is closed exactly once at the terminal page
        """
        try:
            self._populate_listdir(sftp, 20)
            seen = self._command_spy(sftp)
            names = [
                x.filename
                for x in sftp.listdir_iter(sftp.FOLDER, read_aheads=2)
            ]
            assert len(names) == 20
            assert len(set(names)) == 20
            assert seen.count(CMD_CLOSE) == 1
        finally:
            self._cleanup_listdir(sftp, 20)

    @requireNonAsciiLocale()
    def test_listdir_in_locale(self, sftp):
        """Test listdir under a locale that uses non-ascii text."""
        sftp.open(sftp.FOLDER + "/canard.txt", "w").close()
        try:
            folder_contents = sftp.listdir(sftp.FOLDER)
            assert ["canard.txt"] == folder_contents
        finally:
            sftp.remove(sftp.FOLDER + "/canard.txt")

    def test_setstat(self, sftp):
        """
        verify that the setstat functions (chown, chmod, utime, truncate) work.
        """
        try:
            with sftp.open(sftp.FOLDER + "/special", "w") as f:
                f.write("x" * 1024)

            stat = sftp.stat(sftp.FOLDER + "/special")
            sftp.chmod(sftp.FOLDER + "/special", (stat.st_mode & ~o777) | o600)
            stat = sftp.stat(sftp.FOLDER + "/special")
            expected_mode = o600
            if sys.platform == "win32":
                # chmod not really functional on windows
                expected_mode = o666
            if sys.platform == "cygwin":
                # even worse.
                expected_mode = o644
            assert stat.st_mode & o777 == expected_mode
            assert stat.st_size == 1024

            mtime = stat.st_mtime - 3600
            atime = stat.st_atime - 1800
            sftp.utime(sftp.FOLDER + "/special", (atime, mtime))
            stat = sftp.stat(sftp.FOLDER + "/special")
            assert stat.st_mtime == mtime
            if sys.platform not in ("win32", "cygwin"):
                assert stat.st_atime == atime

            # can't really test chown, since we'd have to know a valid uid.

            sftp.truncate(sftp.FOLDER + "/special", 512)
            stat = sftp.stat(sftp.FOLDER + "/special")
            assert stat.st_size == 512
        finally:
            sftp.remove(sftp.FOLDER + "/special")

    def test_fsetstat(self, sftp):
        """
        verify that the fsetstat functions (chown, chmod, utime, truncate)
        work on open files.
        """
        try:
            with sftp.open(sftp.FOLDER + "/special", "w") as f:
                f.write("x" * 1024)

            with sftp.open(sftp.FOLDER + "/special", "r+") as f:
                stat = f.stat()
                f.chmod((stat.st_mode & ~o777) | o600)
                stat = f.stat()

                expected_mode = o600
                if sys.platform == "win32":
                    # chmod not really functional on windows
                    expected_mode = o666
                if sys.platform == "cygwin":
                    # even worse.
                    expected_mode = o644
                assert stat.st_mode & o777 == expected_mode
                assert stat.st_size == 1024

                mtime = stat.st_mtime - 3600
                atime = stat.st_atime - 1800
                f.utime((atime, mtime))
                stat = f.stat()
                assert stat.st_mtime == mtime
                if sys.platform not in ("win32", "cygwin"):
                    assert stat.st_atime == atime

                # can't really test chown, since we'd have to know a valid uid.

                f.truncate(512)
                stat = f.stat()
                assert stat.st_size == 512
        finally:
            sftp.remove(sftp.FOLDER + "/special")

    def test_readline_seek(self, sftp):
        """
        create a text file and write a bunch of text into it.  then count the
        lines in the file, and seek around to retrieve particular lines.  this
        should verify that read buffering and 'tell' work well together, and
        that read buffering is reset on 'seek'.
        """
        try:
            with sftp.open(sftp.FOLDER + "/duck.txt", "w") as f:
                f.write(ARTICLE)

            with sftp.open(sftp.FOLDER + "/duck.txt", "r+") as f:
                line_number = 0
                loc = 0
                pos_list = []
                for line in f:
                    line_number += 1
                    pos_list.append(loc)
                    loc = f.tell()
                assert f.seekable()
                f.seek(pos_list[6], f.SEEK_SET)
                assert f.readline(), "Nouzilly == France.\n"
                f.seek(pos_list[17], f.SEEK_SET)
                assert f.readline()[:4] == "duck"
                f.seek(pos_list[10], f.SEEK_SET)
                expected = "duck types were equally resistant to exogenous insulin compared with chicken.\n"  # noqa
                assert f.readline() == expected
        finally:
            sftp.remove(sftp.FOLDER + "/duck.txt")

    def test_write_seek(self, sftp):
        """
        Create a text file, seek back, change it, and verify.
        """
        try:
            with sftp.open(sftp.FOLDER + "/testing.txt", "w") as f:
                f.write("hello kitty.\n")
                f.seek(-5, f.SEEK_CUR)
                f.write("dd")

            assert sftp.stat(sftp.FOLDER + "/testing.txt").st_size == 13
            with sftp.open(sftp.FOLDER + "/testing.txt", "r") as f:
                data = f.read(20)
            assert data == b"hello kiddy.\n"
        finally:
            sftp.remove(sftp.FOLDER + "/testing.txt")

    def test_symlink(self, sftp):
        """
        create a symlink and then check that lstat doesn't follow it.
        """
        if not hasattr(os, "symlink"):
            # skip symlink tests on windows
            return

        try:
            with sftp.open(sftp.FOLDER + "/original.txt", "w") as f:
                f.write("original\n")
            sftp.symlink("original.txt", sftp.FOLDER + "/link.txt")
            assert sftp.readlink(sftp.FOLDER + "/link.txt") == "original.txt"

            with sftp.open(sftp.FOLDER + "/link.txt", "r") as f:
                assert f.readlines() == ["original\n"]

            cwd = sftp.normalize(".")
            if cwd[-1] == "/":
                cwd = cwd[:-1]
            abs_path = cwd + "/" + sftp.FOLDER + "/original.txt"
            sftp.symlink(abs_path, sftp.FOLDER + "/link2.txt")
            assert abs_path == sftp.readlink(sftp.FOLDER + "/link2.txt")

            assert sftp.lstat(sftp.FOLDER + "/link.txt").st_size == 12
            assert sftp.stat(sftp.FOLDER + "/link.txt").st_size == 9
            # the sftp server may be hiding extra path members from us, so the
            # length may be longer than we expect:
            assert sftp.lstat(sftp.FOLDER + "/link2.txt").st_size >= len(
                abs_path
            )
            assert sftp.stat(sftp.FOLDER + "/link2.txt").st_size == 9
            assert sftp.stat(sftp.FOLDER + "/original.txt").st_size == 9
        finally:
            try:
                sftp.remove(sftp.FOLDER + "/link.txt")
            except:
                pass
            try:
                sftp.remove(sftp.FOLDER + "/link2.txt")
            except:
                pass
            try:
                sftp.remove(sftp.FOLDER + "/original.txt")
            except:
                pass

    def test_flush_seek(self, sftp):
        """
        verify that buffered writes are automatically flushed on seek.
        """
        try:
            with sftp.open(sftp.FOLDER + "/happy.txt", "w", 1) as f:
                f.write("full line.\n")
                f.write("partial")
                f.seek(9, f.SEEK_SET)
                f.write("?\n")

            with sftp.open(sftp.FOLDER + "/happy.txt", "r") as f:
                assert f.readline() == u("full line?\n")
                assert f.read(7) == b"partial"
        finally:
            try:
                sftp.remove(sftp.FOLDER + "/happy.txt")
            except:
                pass

    def test_realpath(self, sftp):
        """
        test that realpath is returning something non-empty and not an
        error.
        """
        pwd = sftp.normalize(".")
        assert len(pwd) > 0
        f = sftp.normalize("./" + sftp.FOLDER)
        assert len(f) > 0
        assert os.path.join(pwd, sftp.FOLDER) == f

    def test_mkdir(self, sftp):
        """
        verify that mkdir/rmdir work.
        """
        sftp.mkdir(sftp.FOLDER + "/subfolder")
        with pytest.raises(IOError):  # generic msg only
            sftp.mkdir(sftp.FOLDER + "/subfolder")
        sftp.rmdir(sftp.FOLDER + "/subfolder")
        with pytest.raises(IOError, match="No such file"):
            sftp.rmdir(sftp.FOLDER + "/subfolder")

    def test_chdir(self, sftp):
        """
        verify that chdir/getcwd work.
        """
        root = sftp.normalize(".")
        if root[-1] != "/":
            root += "/"
        try:
            sftp.mkdir(sftp.FOLDER + "/alpha")
            sftp.chdir(sftp.FOLDER + "/alpha")
            sftp.mkdir("beta")
            assert root + sftp.FOLDER + "/alpha" == sftp.getcwd()
            assert ["beta"] == sftp.listdir(".")

            sftp.chdir("beta")
            with sftp.open("fish", "w") as f:
                f.write("hello\n")
            sftp.chdir("..")
            assert ["fish"] == sftp.listdir("beta")
            sftp.chdir("..")
            assert ["fish"] == sftp.listdir("alpha/beta")
        finally:
            sftp.chdir(root)
            try:
                sftp.unlink(sftp.FOLDER + "/alpha/beta/fish")
            except:
                pass
            try:
                sftp.rmdir(sftp.FOLDER + "/alpha/beta")
            except:
                pass
            try:
                sftp.rmdir(sftp.FOLDER + "/alpha")
            except:
                pass

    def test_get_put(self, sftp):
        """
        verify that get/put work.
        """
        warnings.filterwarnings("ignore", "tempnam.*")

        fd, localname = mkstemp()
        os.close(fd)
        text = b"All I wanted was a plastic bunny rabbit.\n"
        with open(localname, "wb") as f:
            f.write(text)
        saved_progress = []

        def progress_callback(x, y):
            saved_progress.append((x, y))

        sftp.put(localname, sftp.FOLDER + "/bunny.txt", progress_callback)

        with sftp.open(sftp.FOLDER + "/bunny.txt", "rb") as f:
            assert text == f.read(128)
        assert [(41, 41)] == saved_progress

        os.unlink(localname)
        fd, localname = mkstemp()
        os.close(fd)
        saved_progress = []
        sftp.get(sftp.FOLDER + "/bunny.txt", localname, progress_callback)

        with open(localname, "rb") as f:
            assert text == f.read(128)
        assert [(41, 41)] == saved_progress

        os.unlink(localname)
        sftp.unlink(sftp.FOLDER + "/bunny.txt")

    def test_get_without_prefetch(self, sftp):
        """
        Create a 4MB file. Verify that pull works without prefetching
        using a lager file.
        """

        sftp_filename = sftp.FOLDER + "/dummy_file"
        num_chars = 1024 * 1024 * 4

        fd, localname = mkstemp()
        os.close(fd)

        with open(localname, "wb") as f:
            f.write(b"0" * num_chars)

        sftp.put(localname, sftp_filename)

        os.unlink(localname)
        fd, localname = mkstemp()
        os.close(fd)

        sftp.get(sftp_filename, localname, prefetch=False)

        assert os.stat(localname).st_size == num_chars

        os.unlink(localname)
        sftp.unlink(sftp_filename)

    def test_check(self, sftp):
        """
        verify that file.check() works against our own server.
        (it's an sftp extension that we support, and may be the only ones who
        support it.)
        """
        with sftp.open(sftp.FOLDER + "/kitty.txt", "w") as f:
            f.write("here kitty kitty" * 64)

        try:
            with sftp.open(sftp.FOLDER + "/kitty.txt", "r") as f:
                sum = f.check("sha1")
                assert (
                    "91059CFC6615941378D413CB5ADAF4C5EB293402"
                    == u(hexlify(sum)).upper()
                )
                sum = f.check("md5", 0, 512)
                assert (
                    "93DE4788FCA28D471516963A1FE3856A"
                    == u(hexlify(sum)).upper()
                )
                sum = f.check("md5", 0, 0, 510)
                expected = "EB3B45B8CD55A0707D99B177544A319F373183D241432BB2157AB9E46358C4AC90370B5CADE5D90336FC1716F90B36D6"  # noqa
                assert u(hexlify(sum)).upper() == expected
        finally:
            sftp.unlink(sftp.FOLDER + "/kitty.txt")

    def test_x_flag(self, sftp):
        """
        verify that the 'x' flag works when opening a file.
        """
        sftp.open(sftp.FOLDER + "/unusual.txt", "wx").close()

        try:
            with pytest.raises(IOError):
                sftp.open(sftp.FOLDER + "/unusual.txt", "wx")
        finally:
            sftp.unlink(sftp.FOLDER + "/unusual.txt")

    def test_utf8(self, sftp):
        """
        verify that unicode strings are encoded into utf8 correctly.
        """
        with sftp.open(sftp.FOLDER + "/something", "w") as f:
            f.write("okay")
        try:
            sftp.rename(
                sftp.FOLDER + "/something", sftp.FOLDER + "/" + unicode_folder
            )
            sftp.open(b(sftp.FOLDER) + utf8_folder, "r")
        finally:
            sftp.unlink(b(sftp.FOLDER) + utf8_folder)

    def test_utf8_chdir(self, sftp):
        sftp.mkdir(sftp.FOLDER + "/" + unicode_folder)
        try:
            sftp.chdir(sftp.FOLDER + "/" + unicode_folder)
            with sftp.open("something", "w") as f:
                f.write("okay")
            sftp.unlink("something")
        finally:
            sftp.chdir()
            sftp.rmdir(sftp.FOLDER + "/" + unicode_folder)

    def test_bad_readv(self, sftp):
        """
        verify that readv at the end of the file doesn't essplode.
        """
        sftp.open(sftp.FOLDER + "/zero", "w").close()
        try:
            with sftp.open(sftp.FOLDER + "/zero", "r") as f:
                f.readv([(0, 12)])

            with sftp.open(sftp.FOLDER + "/zero", "r") as f:
                file_size = f.stat().st_size
                f.prefetch(file_size)
                f.read(100)
        finally:
            sftp.unlink(sftp.FOLDER + "/zero")

    def test_put_without_confirm(self, sftp):
        """
        verify that get/put work without confirmation.
        """
        warnings.filterwarnings("ignore", "tempnam.*")

        fd, localname = mkstemp()
        os.close(fd)
        text = b"All I wanted was a plastic bunny rabbit.\n"
        with open(localname, "wb") as f:
            f.write(text)
        saved_progress = []

        def progress_callback(x, y):
            saved_progress.append((x, y))

        res = sftp.put(
            localname, sftp.FOLDER + "/bunny.txt", progress_callback, False
        )

        assert SFTPAttributes().attr == res.attr

        with sftp.open(sftp.FOLDER + "/bunny.txt", "r") as f:
            assert text == f.read(128)
        assert (41, 41) == saved_progress[-1]

        os.unlink(localname)
        sftp.unlink(sftp.FOLDER + "/bunny.txt")

    def test_getcwd(self, sftp):
        """
        verify that chdir/getcwd work.
        """
        assert sftp.getcwd() is None
        root = sftp.normalize(".")
        if root[-1] != "/":
            root += "/"
        try:
            sftp.mkdir(sftp.FOLDER + "/alpha")
            sftp.chdir(sftp.FOLDER + "/alpha")
            assert sftp.getcwd() == "/" + sftp.FOLDER + "/alpha"
        finally:
            sftp.chdir(root)
            try:
                sftp.rmdir(sftp.FOLDER + "/alpha")
            except:
                pass

    def test_seek_append(self, sftp):
        """
        verify that seek doesn't affect writes during append.

        does not work except through paramiko.  :(  openssh fails.
        """
        try:
            with sftp.open(sftp.FOLDER + "/append.txt", "a") as f:
                f.write("first line\nsecond line\n")
                f.seek(11, f.SEEK_SET)
                f.write("third line\n")

            with sftp.open(sftp.FOLDER + "/append.txt", "r") as f:
                assert f.stat().st_size == 34
                assert f.readline() == "first line\n"
                assert f.readline() == "second line\n"
                assert f.readline() == "third line\n"
        finally:
            sftp.remove(sftp.FOLDER + "/append.txt")

    def test_putfo_empty_file(self, sftp):
        """
        Send an empty file and confirm it is sent.
        """
        target = sftp.FOLDER + "/empty file.txt"
        stream = StringIO()
        try:
            attrs = sftp.putfo(stream, target)
            # the returned attributes should not be null
            assert attrs is not None
        finally:
            sftp.remove(target)

    # TODO: this test doesn't actually fail if the regression (removing '%'
    # expansion to '%%' within sftp.py's def _log()) is removed - stacktraces
    # appear but they're clearly emitted from subthreads that have no error
    # handling. No point running it until that is fixed somehow.
    @pytest.mark.skip("Doesn't prove anything right now")
    def test_file_with_percent(self, sftp):
        """
        verify that we can create a file with a '%' in the filename.
        ( it needs to be properly escaped by _log() )
        """
        f = sftp.open(sftp.FOLDER + "/test%file", "w")
        try:
            assert f.stat().st_size == 0
        finally:
            f.close()
            sftp.remove(sftp.FOLDER + "/test%file")

    def test_non_utf8_data(self, sftp):
        """Test write() and read() of non utf8 data"""
        try:
            with sftp.open(f"{sftp.FOLDER}/nonutf8data", "w") as f:
                f.write(NON_UTF8_DATA)
            with sftp.open(f"{sftp.FOLDER}/nonutf8data", "r") as f:
                data = f.read()
            assert data == NON_UTF8_DATA
            with sftp.open(f"{sftp.FOLDER}/nonutf8data", "wb") as f:
                f.write(NON_UTF8_DATA)
            with sftp.open(f"{sftp.FOLDER}/nonutf8data", "rb") as f:
                data = f.read()
            assert data == NON_UTF8_DATA
        finally:
            sftp.remove(f"{sftp.FOLDER}/nonutf8data")

    @requireNonAsciiLocale("LC_TIME")
    def test_sftp_attributes_locale_time(self, sftp):
        """Test SFTPAttributes under a locale with non-ascii time strings."""
        some_stat = os.stat(sftp.FOLDER)
        sftp_attributes = SFTPAttributes.from_stat(some_stat, u("a_directory"))
        assert b"a_directory" in sftp_attributes.asbytes()

    def test_sftp_attributes_empty_str(self, sftp):
        sftp_attributes = SFTPAttributes()
        assert (
            str(sftp_attributes)
            == "?---------   1 0        0               0 (unknown date) ?"
        )

    @needs_builtin("buffer")
    def test_write_buffer(self, sftp):
        """Test write() using a buffer instance."""
        data = 3 * b"A potentially large block of data to chunk up.\n"
        try:
            with sftp.open(f"{sftp.FOLDER}/write_buffer", "wb") as f:
                for offset in range(0, len(data), 8):
                    f.write(buffer(data, offset, 8))  # noqa

            with sftp.open(f"{sftp.FOLDER}/write_buffer", "rb") as f:
                assert f.read() == data
        finally:
            sftp.remove(f"{sftp.FOLDER}/write_buffer")

    @needs_builtin("memoryview")
    def test_write_memoryview(self, sftp):
        """Test write() using a memoryview instance."""
        data = 3 * b"A potentially large block of data to chunk up.\n"
        try:
            with sftp.open(f"{sftp.FOLDER}/write_memoryview", "wb") as f:
                view = memoryview(data)
                for offset in range(0, len(data), 8):
                    f.write(view[offset : offset + 8])

            with sftp.open(f"{sftp.FOLDER}/write_memoryview", "rb") as f:
                assert f.read() == data
        finally:
            sftp.remove(f"{sftp.FOLDER}/write_memoryview")


# Scripted SFTP response packet builders (payload includes the request id,
# exactly as BaseSFTP._read_packet returns it).


def _handle_response(num, handle=b"dir-handle"):
    msg = Message()
    msg.add_int(num)
    msg.add_string(handle)
    return CMD_HANDLE, msg.asbytes()


def _name_response(num, names):
    msg = Message()
    msg.add_int(num)
    msg.add_int(len(names))
    for name in names:
        msg.add_string(name)
        msg.add_string("")
        SFTPAttributes()._pack(msg)
    return CMD_NAME, msg.asbytes()


def _status_response(num, code, text=""):
    msg = Message()
    msg.add_int(num)
    msg.add_int(code)
    msg.add_string(text)
    msg.add_string("")
    return CMD_STATUS, msg.asbytes()


class FakeSFTPClient(BaseSFTP):
    """
    Minimal SFTPClient stand-in that serves a scripted packet stream and
    records outgoing commands, while reusing the real response/status logic.
    """

    _read_response = SFTPClient._read_response
    _convert_status = SFTPClient._convert_status

    def __init__(self, responses=()):
        super().__init__()
        self._lock = threading.Lock()
        self._cwd = None
        self.request_number = 1
        self._expecting = weakref.WeakValueDictionary()
        self._open_directories = weakref.WeakSet()
        self._responses = deque(responses)
        self.commands = []
        self.closed = False

    def _adjust_cwd(self, path):
        return path

    def _async_request(self, fileobj, t, *args):
        with self._lock:
            num = self.request_number
            self.request_number += 1
            self._expecting[num] = fileobj
        self.commands.append((t, args))
        return num

    def _request(self, t, *args):
        num = self._async_request(type(None), t, *args)
        return self._read_response(num)

    def _read_packet(self):
        return self._responses.popleft()

    def close(self):
        for directory in list(self._open_directories):
            directory.close()
        self.closed = True


class TestSFTPDirectoryIterator:
    """deterministic, network-free tests of handle lifecycle semantics"""

    def test_full_listing_closes_once_and_drains_eof(self):
        fake = FakeSFTPClient(
            [
                _handle_response(1),
                _name_response(2, ["a", "b"]),
                _name_response(3, ["c"]),
                _status_response(4, SFTP_EOF, "End of file"),
                _status_response(5, SFTP_OK),
            ]
        )
        iterator = _SFTPDirectoryIterator(fake, ".", read_aheads=3)
        assert [x.filename for x in iterator] == ["a", "b", "c"]
        # exactly one close, sent after the readdir batch, and every
        # scripted packet (including the trailing EOFs) was consumed
        assert [t for t, args in fake.commands] == [
            CMD_OPENDIR,
            CMD_READDIR,
            CMD_READDIR,
            CMD_READDIR,
            CMD_CLOSE,
        ]
        assert not fake._responses
        assert not fake._expecting

    def test_early_close_drains_and_keeps_client_usable(self):
        fake = FakeSFTPClient(
            [
                _handle_response(1),
                _name_response(2, ["a", "b", "c"]),
                _status_response(3, SFTP_EOF),
                # a server racing CLOSE with the last page may answer the
                # remaining pipelined readdirs with an error; it must be
                # discarded, not raised as a listing/network error
                _status_response(4, SFTP_FAILURE, "Invalid handle"),
                _status_response(5, SFTP_OK),
            ]
        )
        iterator = _SFTPDirectoryIterator(fake, ".", read_aheads=3)
        assert next(iterator).filename == "a"
        iterator.close()
        iterator.close()  # idempotent
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1
        assert not fake._responses
        assert not fake._expecting
        # entries already buffered are never delivered twice
        with pytest.raises(StopIteration):
            next(iterator)
        assert list(iterator) == []

        # the same client immediately serves an unrelated request correctly
        fake._responses.append(_handle_response(6))
        t, msg = fake._request(CMD_OPEN, "other-file")
        assert t == CMD_HANDLE
        assert not fake._responses

    def test_garbage_collection_reaps_handle(self):
        fake = FakeSFTPClient(
            [
                _handle_response(1),
                _name_response(2, ["a", "b"]),
                _status_response(3, SFTP_EOF),
                _status_response(4, SFTP_EOF),
                _status_response(5, SFTP_OK),
            ]
        )

        def abandon():
            iterator = _SFTPDirectoryIterator(fake, ".", read_aheads=3)
            next(iterator)
            # dropped without an explicit close

        abandon()
        gc.collect()
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1
        assert not fake._responses
        assert not fake._expecting
        gc.collect()
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1

    def test_exception_in_caller_reaps_handle(self):
        fake = FakeSFTPClient(
            [
                _handle_response(1),
                _name_response(2, ["a", "b"]),
                _status_response(3, SFTP_EOF),
                _status_response(4, SFTP_EOF),
                _status_response(5, SFTP_OK),
            ]
        )
        with pytest.raises(RuntimeError):
            with _SFTPDirectoryIterator(fake, ".", read_aheads=3) as it:
                assert next(it).filename == "a"
                raise RuntimeError("boom")
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1
        assert not fake._responses
        with pytest.raises(StopIteration):
            next(it)

    def test_mid_listing_server_error_still_closes(self):
        fake = FakeSFTPClient(
            [
                _handle_response(1),
                _status_response(2, SFTP_FAILURE, "nope"),
                _status_response(3, SFTP_EOF),
                _status_response(4, SFTP_FAILURE, "Invalid handle"),
                _status_response(5, SFTP_OK),
            ]
        )
        iterator = _SFTPDirectoryIterator(fake, ".", read_aheads=3)
        with pytest.raises(IOError):
            next(iterator)
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1
        assert not fake._responses
        assert not fake._expecting

    def test_client_close_reaps_unconsumed_iterator(self):
        fake = FakeSFTPClient(
            [_handle_response(1), _status_response(2, SFTP_OK)]
        )
        iterator = _SFTPDirectoryIterator(fake, ".")
        # never iterated: no readdir requests, but the open handle is
        # released when the client goes down
        fake.close()
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1
        assert not fake._responses
        assert fake.closed
        with pytest.raises(StopIteration):
            next(iterator)

    def test_paginated_entries_are_delivered_exactly_once(self):
        fake = FakeSFTPClient(
            [
                _handle_response(1),
                _name_response(2, ["a", "b"]),
                _name_response(3, ["c", "d"]),
                _status_response(4, SFTP_EOF),
                _status_response(5, SFTP_EOF),
                _status_response(6, SFTP_OK),
            ]
        )
        # read_aheads=2 forces multiple batches; second batch is two EOFs
        iterator = _SFTPDirectoryIterator(fake, ".", read_aheads=2)
        names = [x.filename for x in iterator]
        assert sorted(names) == ["a", "b", "c", "d"]
        assert len(names) == len(set(names))
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1
        assert not fake._responses

    def test_normal_eof_is_not_reported_as_network_error(self):
        fake = FakeSFTPClient(
            [
                _handle_response(1),
                _status_response(2, SFTP_EOF, "End of file"),
                _status_response(3, SFTP_EOF),
                _status_response(4, SFTP_EOF),
                _status_response(5, SFTP_OK),
            ]
        )
        iterator = _SFTPDirectoryIterator(fake, ".", read_aheads=3)
        # empty directory: completes silently, no exception of any kind
        assert list(iterator) == []
        assert sum(t == CMD_CLOSE for t, args in fake.commands) == 1
        assert not fake._responses
