from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from datetime import datetime, timedelta, timezone
import unittest

from host.player_process_composition import WindowsProcessIdentityReader, windows_filetime_start_time_utc


def exact_ticks(instant: datetime, remainder: int = 0) -> int:
    span = instant - datetime(1601, 1, 1, tzinfo=timezone.utc)
    return ((span.days * 86400 + span.seconds) * 1_000_000 + span.microseconds) * 10 + remainder


class KernelDouble:
    def __init__(self, ticks: int):
        self.ticks = ticks
        self.times_calls = self.image_calls = 0

    def GetProcessTimes(self, handle, creation, exit_time, kernel_time, user_time):
        self.times_calls += 1
        row = ctypes.cast(creation, ctypes.POINTER(wintypes.FILETIME)).contents
        row.dwHighDateTime = self.ticks >> 32
        row.dwLowDateTime = self.ticks & 0xFFFFFFFF
        return True

    def QueryFullProcessImageNameW(self, handle, flags, buffer, length):
        self.image_calls += 1
        buffer.value = r'C:\Synthetic\ExamplePlayer.exe'
        ctypes.cast(length, ctypes.POINTER(wintypes.DWORD)).contents.value = len(buffer.value)
        return True


class WindowsFileTimeIdentityTests(unittest.TestCase):
    def test_exact_submicrosecond_digit_does_not_round_identity(self):
        instant = datetime(2032, 6, 12, 9, 8, 7, 161932, tzinfo=timezone.utc)
        ticks = exact_ticks(instant, 9)
        old = datetime.fromtimestamp((ticks - 116444736000000000) / 10_000_000, timezone.utc)
        self.assertNotEqual(old.microsecond, instant.microsecond)  # Actual float failure, synthetic date.
        for remainder in range(10):
            self.assertEqual(windows_filetime_start_time_utc(exact_ticks(instant, remainder)), '2032-06-12T09:08:07.161932Z')

    def test_native_reader_uses_exact_high_low_words_and_preserves_pid_path(self):
        kernel = KernelDouble(exact_ticks(datetime(2032, 6, 12, 9, 8, 7, 161932, tzinfo=timezone.utc), 9))
        reader = object.__new__(WindowsProcessIdentityReader)
        reader._kernel32 = kernel
        identity = reader.read_handle(1, 4200)
        self.assertEqual(identity.as_dict(), {'processId': 4200, 'startTimeUtc': '2032-06-12T09:08:07.161932Z', 'executablePath': r'C:\Synthetic\ExamplePlayer.exe'})
        self.assertEqual((kernel.times_calls, kernel.image_calls), (1, 1))

    def test_second_day_and_epoch_boundaries_use_integer_truncation(self):
        instants = [datetime(1601, 1, 1, tzinfo=timezone.utc), datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
                    datetime(1970, 1, 1, tzinfo=timezone.utc), datetime(2032, 2, 29, 23, 59, 59, 999999, tzinfo=timezone.utc)]
        for instant in instants:
            for delta in (-1, 0, 1, 9, 10):
                ticks = exact_ticks(instant) + delta
                if ticks < 0:
                    continue
                expected = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
                self.assertEqual(windows_filetime_start_time_utc(ticks), expected.isoformat(timespec='microseconds').replace('+00:00', 'Z'))

    def test_invalid_or_unrepresentable_native_values_rejected(self):
        for value in (True, False, 1.0, '10', -1, 1 << 64):
            with self.assertRaises(ValueError):
                windows_filetime_start_time_utc(value)
        with self.assertRaises(OverflowError):
            windows_filetime_start_time_utc((1 << 64) - 1)

    def test_one_microsecond_or_pid_difference_remains_a_real_mismatch(self):
        instant = datetime(2032, 6, 12, 9, 8, 7, 161932, tzinfo=timezone.utc)
        self.assertNotEqual(windows_filetime_start_time_utc(exact_ticks(instant)), windows_filetime_start_time_utc(exact_ticks(instant) + 10))
        kernel = KernelDouble(exact_ticks(instant))
        reader = object.__new__(WindowsProcessIdentityReader)
        reader._kernel32 = kernel
        self.assertNotEqual(reader.read_handle(1, 4200), reader.read_handle(1, 4201))
        self.assertIsNone(reader.read_handle(1, True))
        self.assertIsNone(reader.read_handle(None, 4200))


if __name__ == '__main__':
    unittest.main()
