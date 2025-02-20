#  type: ignore
"""R/W implementation of packet parsing.

Doesn't rely on custom C++ extensions (just numpy). Provides writable
view of packet data for testing and development.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import (Callable, ClassVar, Dict, Type, Union, List, Iterator,
                    Optional)

import numpy as np

import ouster.client as client
from ouster.client import (ChanField, ColHeader, FieldDType, SensorInfo,
                           UDPProfileLidar)

_legacy_scan_fields: Dict[ChanField, FieldDType] = {
    ChanField.RANGE: np.uint32,
    ChanField.SIGNAL: np.uint32,
    ChanField.NEAR_IR: np.uint32,
    ChanField.REFLECTIVITY: np.uint32,
}

_lb_scan_fields: Dict[ChanField, FieldDType] = {
    ChanField.RANGE: np.uint32,
    ChanField.REFLECTIVITY: np.uint16,
    ChanField.NEAR_IR: np.uint16,
}

_single_scan_fields: Dict[ChanField, FieldDType] = {
    ChanField.RANGE: np.uint32,
    ChanField.SIGNAL: np.uint16,
    ChanField.NEAR_IR: np.uint16,
    ChanField.REFLECTIVITY: np.uint16,
}

_dual_scan_fields: Dict[ChanField, FieldDType] = {
    ChanField.RANGE: np.uint32,
    ChanField.RANGE2: np.uint32,
    ChanField.SIGNAL: np.uint16,
    ChanField.SIGNAL2: np.uint16,
    ChanField.REFLECTIVITY: np.uint16,
    ChanField.REFLECTIVITY2: np.uint16,
    ChanField.NEAR_IR: np.uint16,
}

_five_word_pixel_fields: Dict[ChanField, FieldDType] = {
    ChanField.RANGE: np.uint32,
    ChanField.RANGE2: np.uint32,
    ChanField.SIGNAL: np.uint16,
    ChanField.SIGNAL2: np.uint16,
    ChanField.REFLECTIVITY: np.uint16,
    ChanField.REFLECTIVITY2: np.uint16,
    ChanField.NEAR_IR: np.uint16,
    ChanField.RAW32_WORD1: np.uint32,
    ChanField.RAW32_WORD2: np.uint32,
    ChanField.RAW32_WORD3: np.uint32,
    ChanField.RAW32_WORD4: np.uint32,
    ChanField.RAW32_WORD5: np.uint32,
}


# TODO[pb]: This method should be removed and replaced with smth that matches
#           the states of the profiles in C++
def default_scan_fields(
        profile: UDPProfileLidar,
        flags: bool = False,
        raw_headers: bool = False) -> Optional[Dict[ChanField, FieldDType]]:
    """Get the default fields populated on scans for a profile.

    Convenient helper function if you want to tweak which fields are parsed
    into a LidarScan without listing out the defaults yourself.

    Args:
        profile: The lidar profile
        flags: Include the FLAGS fields
        raw_headers: Include RAW_HEADERS field

    Returns:
        A field configuration that can be passed to `client.Scans`. or None for
        custom added UDPProfileLidar
    """
    profile_fields = {
        UDPProfileLidar.PROFILE_LIDAR_LEGACY:
        _legacy_scan_fields,
        UDPProfileLidar.PROFILE_LIDAR_RNG15_RFL8_NIR8:
        _lb_scan_fields,
        UDPProfileLidar.PROFILE_LIDAR_RNG19_RFL8_SIG16_NIR16:
        _single_scan_fields,
        UDPProfileLidar.PROFILE_LIDAR_RNG19_RFL8_SIG16_NIR16_DUAL:
        _dual_scan_fields,
        UDPProfileLidar.PROFILE_LIDAR_FIVE_WORD_PIXEL:
        _five_word_pixel_fields
    }

    # bail if it's some new added custom profile
    if profile not in profile_fields:
        return None

    fields = profile_fields[profile]

    if flags:
        fields.update({ChanField.FLAGS: np.uint8})
        if profile == UDPProfileLidar.PROFILE_LIDAR_RNG19_RFL8_SIG16_NIR16_DUAL:
            fields.update({ChanField.FLAGS2: np.uint8})

    if raw_headers:
        # Getting the optimal field type for RAW_HEADERS is not possible with
        # this method thus using the biggest UINT32 for RAW_HEADERS.

        # Alternatively you can use `osf.resolve_field_types()` that chooses
        # the more optimal dtype for RAW_HEADERS field
        fields.update({ChanField.RAW_HEADERS: np.uint32})

    return fields.copy()


@dataclass
class FieldDescr:
    offset: int
    dtype: type
    mask: int = 0
    shift: int = 0


class MaskedView:

    def __init__(self, data: np.ndarray, pf: 'PacketFormat',
                 field: Union[ColHeader, ChanField]) -> None:
        if len(data) < pf.lidar_packet_size:
            raise ValueError("Packet buffer smaller than expected size")
        if isinstance(field, ChanField):
            f = pf._FIELDS[field]
            self._data = np.lib.stride_tricks.as_strided(
                data[pf._packet_header_size + pf._col_header_size +
                     f.offset:].view(dtype=f.dtype),
                shape=(pf._pixels_per_column, pf._columns_per_packet),
                strides=(pf._channel_data_size, pf.column_size))
        elif isinstance(field, ColHeader):
            f = pf._HEADERS[field]
            start = 0 if f.offset >= 0 else pf.column_size
            self._data = np.lib.stride_tricks.as_strided(
                data[pf._packet_header_size + f.offset +
                     start:].view(dtype=f.dtype),
                shape=(pf._columns_per_packet, ),
                strides=(pf.column_size, ))

        self._mask = f.mask
        self._shift = f.shift

    def __getitem__(self, key) -> np.ndarray:
        value = self._data.__getitem__(key)
        if self._mask:
            value = value & self._mask
        if self._shift > 0:
            value = value >> self._shift
        elif self._shift < 0:
            value = value << abs(self._shift)
        return value

    def __setitem__(self, key, value) -> None:
        # TODO: how to support operators for MaskedView
        if self._shift > 0:
            value = np.left_shift(value, self._shift)
        elif self._shift < 0:
            value = np.right_shift(value, abs(self._shift))
        if self._mask:
            old = self._data.__getitem__(key)
            value = np.bitwise_and(value, self._mask) | np.bitwise_and(
                old, ~self._mask)
        self._data.__setitem__(key, value)

    def __len__(self) -> int:
        return self._data.__len__()

    def __repr__(self) -> str:
        return self._data.__repr__()

    def __getattribute__(self, att):
        if att in ['_data', '_mask', '_shift']:
            return object.__getattribute__(self, att)
        return getattr(self._data, att)


class PacketFormat(ABC):
    """Read lidar packet data using numpy views."""

    _pixels_per_column: int
    _columns_per_packet: int

    _packet_header_size: int
    _packet_footer_size: int
    _col_header_size: int
    _col_footer_size: int
    _channel_data_size: int

    column_size: int
    lidar_packet_size: int

    _HEADERS: ClassVar[Dict[ColHeader, FieldDescr]] = {}
    _FIELDS: ClassVar[Dict[ChanField, FieldDescr]] = {}

    def __init__(self, *, packet_header_size: int, columns_per_packet: int,
                 col_header_size: int, pixels_per_column: int,
                 channel_data_size: int, col_footer_size: int,
                 packet_footer_size: int) -> None:

        self._packet_header_size = packet_header_size
        self._columns_per_packet = columns_per_packet
        self._col_header_size = col_header_size
        self._pixels_per_column = pixels_per_column
        self._channel_data_size = channel_data_size
        self._col_footer_size = col_footer_size
        self._packet_footer_size = packet_footer_size

        self.column_size = (self._col_header_size +
                            self._pixels_per_column * self._channel_data_size +
                            self._col_footer_size)
        self.lidar_packet_size = (self._packet_header_size +
                                  self._columns_per_packet * self.column_size +
                                  self._packet_footer_size)

    # blah ... could make formats a view and these properties
    @abstractmethod
    def packet_type(self, data: np.ndarray) -> int:
        ...

    @abstractmethod
    def set_packet_type(self, data: np.ndarray, val: int) -> None:
        ...

    @abstractmethod
    def frame_id(self, data: np.ndarray) -> int:
        ...

    @abstractmethod
    def set_frame_id(self, data: np.ndarray, val: int) -> None:
        ...

    @abstractmethod
    def init_id(self, data: np.ndarray) -> int:
        ...

    @abstractmethod
    def set_init_id(self, data: np.ndarray, val: int) -> None:
        ...

    @abstractmethod
    def prod_sn(self, data: np.ndarray) -> int:
        ...

    @abstractmethod
    def set_prod_sn(self, data: np.ndarray, val: int) -> None:
        ...

    def field(self, data: np.ndarray, field: ChanField) -> MaskedView:
        return MaskedView(data, self, field)

    def header(self, data: np.ndarray, header: ColHeader) -> MaskedView:
        return MaskedView(data, self, header)

    @staticmethod
    def from_profile(profile: UDPProfileLidar, pixels_per_column: int,
                     columns_per_packet: int) -> 'PacketFormat':

        formats: Dict[UDPProfileLidar, Callable[[int, int], PacketFormat]] = {
            UDPProfileLidar.PROFILE_LIDAR_LEGACY: LegacyFormat,
            UDPProfileLidar.PROFILE_LIDAR_RNG19_RFL8_SIG16_NIR16_DUAL:
            DualFormat,
            UDPProfileLidar.PROFILE_LIDAR_RNG19_RFL8_SIG16_NIR16: SingleFormat,
            UDPProfileLidar.PROFILE_LIDAR_RNG15_RFL8_NIR8: LBFormat,
        }
        return formats[profile](pixels_per_column, columns_per_packet)

    @staticmethod
    def from_metadata(meta: SensorInfo) -> 'PacketFormat':
        return PacketFormat.from_profile(meta.format.udp_profile_lidar,
                                         meta.format.pixels_per_column,
                                         meta.format.columns_per_packet)

    @staticmethod
    def convertible(a: Type['PacketFormat'], b: Type['PacketFormat']) -> bool:
        """Check if it's possible to convert packets of ``a`` to ``b``."""
        return (all(f in a._FIELDS.keys() for f in b._FIELDS.keys())
                and all(h in a._HEADERS.keys() for h in b._HEADERS.keys()))


class LegacyFormat(PacketFormat):

    _HEADERS: ClassVar[Dict[ColHeader, FieldDescr]] = {
        ColHeader.TIMESTAMP: FieldDescr(offset=0, dtype=np.uint64),
        ColHeader.MEASUREMENT_ID: FieldDescr(8, np.uint16),
        ColHeader.FRAME_ID: FieldDescr(offset=10, dtype=np.uint16),
        ColHeader.ENCODER_COUNT: FieldDescr(12, np.uint32),
        ColHeader.STATUS: FieldDescr(-4, np.uint32),
    }

    _FIELDS: ClassVar[Dict[ChanField, FieldDescr]] = {
        ChanField.RANGE: FieldDescr(0, np.uint32, mask=0x000fffff),
        ChanField.REFLECTIVITY: FieldDescr(4, np.uint16),
        ChanField.SIGNAL: FieldDescr(6, np.uint16),
        ChanField.FLAGS: FieldDescr(3, np.uint8, shift=4),
        ChanField.NEAR_IR: FieldDescr(8, np.uint16),
    }

    def __init__(self, pixels_per_column: int, columns_per_packet: int):
        super().__init__(packet_header_size=0,
                         columns_per_packet=columns_per_packet,
                         col_header_size=16,
                         pixels_per_column=pixels_per_column,
                         channel_data_size=12,
                         col_footer_size=4,
                         packet_footer_size=0)

    def packet_type(self, data: np.ndarray) -> int:
        return 0

    def set_packet_type(self, data: np.ndarray, val: int) -> None:
        pass

    def frame_id(self, data: np.ndarray) -> int:
        return self.header(data, ColHeader.FRAME_ID)[0].item()

    def set_frame_id(self, data: np.ndarray, val: int) -> None:
        self.header(data, ColHeader.FRAME_ID)[:] = val

    def init_id(self, data: np.ndarray) -> int:
        return 0

    def set_init_id(self, data: np.ndarray, val: int) -> None:
        pass

    def prod_sn(self, data: np.ndarray) -> int:
        return 0

    def set_prod_sn(self, data: np.ndarray, val: int) -> None:
        pass


class EUDPFormat(PacketFormat):

    _HEADERS: ClassVar[Dict[ColHeader, FieldDescr]] = {
        ColHeader.TIMESTAMP: FieldDescr(0, np.uint64),
        ColHeader.MEASUREMENT_ID: FieldDescr(8, np.uint16),
        ColHeader.STATUS: FieldDescr(10, np.uint16),
    }

    def __init__(self, pixels_per_column: int, columns_per_packet: int,
                 channel_data_size: int) -> None:
        super().__init__(packet_header_size=32,
                         columns_per_packet=columns_per_packet,
                         col_header_size=12,
                         pixels_per_column=pixels_per_column,
                         channel_data_size=channel_data_size,
                         col_footer_size=0,
                         packet_footer_size=32)

    def packet_type(self, data: np.ndarray) -> int:
        return int.from_bytes(data[0:2].tobytes(), byteorder='little')

    def set_packet_type(self, data: np.ndarray, val: int) -> None:
        data[0:2] = memoryview(val.to_bytes(2, byteorder='little'))

    def frame_id(self, data: np.ndarray) -> int:
        return int.from_bytes(data[2:4].tobytes(), byteorder='little')

    def set_frame_id(self, data: np.ndarray, val: int) -> None:
        data[2:4] = memoryview(val.to_bytes(2, byteorder='little'))

    def init_id(self, data: np.ndarray) -> int:
        return int.from_bytes(data[4:7].tobytes(), byteorder='little')

    def set_init_id(self, data: np.ndarray, val: int) -> None:
        data[4:7] = memoryview(val.to_bytes(3, byteorder='little'))

    def prod_sn(self, data: np.ndarray) -> int:
        return int.from_bytes(data[7:12].tobytes(), byteorder='little')

    def set_prod_sn(self, data: np.ndarray, val: int) -> None:
        data[7:12] = memoryview(val.to_bytes(5, byteorder='little'))


class LBFormat(EUDPFormat):
    """PROFILE_RNG15_RFL8_NIR8"""

    _FIELDS: ClassVar[Dict[ChanField, FieldDescr]] = {
        ChanField.RANGE: FieldDescr(0, np.uint16, mask=0x7fff, shift=-3),
        ChanField.REFLECTIVITY: FieldDescr(2, np.uint8),
        ChanField.NEAR_IR: FieldDescr(3, np.uint8, shift=-4),
    }

    def __init__(self, pixels_per_column: int,
                 columns_per_packet: int) -> None:
        super().__init__(pixels_per_column,
                         columns_per_packet,
                         channel_data_size=4)


class SingleFormat(EUDPFormat):
    """PROFILE_RNG19_RFL8_SIG16_NIR16"""

    _FIELDS: ClassVar[Dict[ChanField, FieldDescr]] = {
        ChanField.RANGE: FieldDescr(0, np.uint32, mask=0x000fffff),
        ChanField.REFLECTIVITY: FieldDescr(4, np.uint16),
        ChanField.SIGNAL: FieldDescr(6, np.uint16),
        ChanField.FLAGS: FieldDescr(3, np.uint8, shift=4),
        ChanField.NEAR_IR: FieldDescr(8, np.uint16),
    }

    def __init__(self, pixels_per_column: int,
                 columns_per_packet: int) -> None:
        super().__init__(pixels_per_column,
                         columns_per_packet,
                         channel_data_size=12)


class DualFormat(EUDPFormat):
    """PROFILE_RNG19_RFL8_SIG16_NIR16_DUAL"""

    _FIELDS: ClassVar[Dict[ChanField, FieldDescr]] = {
        ChanField.RANGE: FieldDescr(0, np.uint32, mask=0x0007ffff),
        ChanField.REFLECTIVITY: FieldDescr(3, np.uint8),
        ChanField.RANGE2: FieldDescr(4, np.uint32, mask=0x0007ffff),
        ChanField.REFLECTIVITY2: FieldDescr(7, np.uint8),
        ChanField.SIGNAL: FieldDescr(8, np.uint16),
        ChanField.SIGNAL2: FieldDescr(10, np.uint16),
        ChanField.FLAGS: FieldDescr(2, np.uint8, mask=0b11111000, shift=3),
        ChanField.FLAGS2: FieldDescr(6, np.uint8, mask=0b11111000, shift=3),
        ChanField.NEAR_IR: FieldDescr(12, np.uint16),
    }

    def __init__(self, pixels_per_column: int, columns_per_packet) -> None:
        super().__init__(pixels_per_column=pixels_per_column,
                         columns_per_packet=columns_per_packet,
                         channel_data_size=16)


def tohex(data: client.BufferT) -> str:
    """Makes a hex string for debug print outs of buffers.

    Selects the biggest devisor of np.uint32, np.uint16 or np.uint8 for making
    a hex output of the provided data. (clunky but usefull for debugging)

    """
    if len(data):
        if isinstance(data, np.ndarray) and not data.flags['C_CONTIGUOUS']:
            data_cont = np.ascontiguousarray(data)
        else:
            data_cont = data
        # selecting the biggest dtype that devides num bytes exactly, because
        # vectorized hex can't work with data if it's not a multiple of element
        # type
        bytes_len = np.frombuffer(data_cont, dtype=np.uint8).size
        dtype = {
            0: np.uint32,
            1: np.uint8,
            2: np.uint16,
            3: np.uint8
        }[bytes_len % 4]
        return np.vectorize(hex)(np.frombuffer(data_cont, dtype=dtype))
    else:
        return "[]"


class LidarPacketHeaders:
    """Gets access to the headers and footers of the lidar packet buffer."""

    def __init__(self, pf: client._client.PacketFormat) -> None:
        self._pf = pf

    def packet_header(self, packet_buf: client.BufferT) -> np.ndarray:
        if self._pf.packet_header_size:
            return self._uint8_view(packet_buf)[:self._pf.packet_header_size]
        else:
            return np.empty(0, dtype=np.uint8)

    def packet_footer(self, packet_buf: client.BufferT) -> np.ndarray:
        if self._pf.packet_footer_size:
            return self._uint8_view(packet_buf)[-self._pf.packet_footer_size:]
        else:
            return np.empty(0, dtype=np.uint8)

    def col_header(self, packet_buf: client.BufferT,
                   col_idx: int) -> np.ndarray:
        if self._pf.col_header_size:
            col_offset = (self._pf.packet_header_size +
                          self._pf.col_size * col_idx)
            return self._uint8_view(packet_buf)[col_offset:col_offset +
                                                self._pf.col_header_size]
        else:
            return np.empty(0, dtype=np.uint8)

    def col_footer(self, packet_buf: client.BufferT,
                   col_idx: int) -> np.ndarray:
        if self._pf.col_footer_size:
            col_end_offset = (self._pf.packet_header_size + self._pf.col_size *
                              (col_idx + 1))
            return self._uint8_view(
                packet_buf)[col_end_offset -
                            self._pf.col_footer_size:col_end_offset]
        else:
            return np.empty(0, dtype=np.uint8)

    def _uint8_view(self, packet_buf: client.BufferT) -> np.ndarray:
        return np.frombuffer(packet_buf,
                             dtype=np.uint8,
                             count=self._pf.lidar_packet_size)


class RawHeadersFormat:
    """Accessor to the single column headers layout.

    ``col_view`` param in every accessor function is a packer column
    from the RAW_HEADERS field, the layout used to pack the packet
    headers and footers is the following:

    -- [ col_header ] [col_footer ] [ packet_header ] [ packet_footer ] --

    ``col_view`` element data type is uint32, thus the conversion is required
    to a uint8 view.

    Also ``col_view`` by default is not a CONTIGUOUS byte buffer because of
    internal LidarScan fields representation as a C (i.e. row major) order,
    thus there is additional handler to convert ``col_view`` buffer into a
    `C_CONTIGUOUS` byte order for a direct buffer accessing operations.

    NOTE[pb]: There definitely other options to make it more efficient with
              numpy ops and Python, but I wasn't able to figure out the
              faster way and there is a know speed issues with such accessors.

              Better alternative would be to implement accessors and
              ``scan_to_buffers`` operations in C++ with a corresponding
              bindings, maybe sometime later when there will be asks ...
    """

    def __init__(self, pf: client._client.PacketFormat) -> None:
        self._pf = pf

    def packet_header(self, col_view: np.ndarray) -> np.ndarray:
        if self._pf.packet_header_size:
            header_offset = self._pf.col_header_size + self._pf.col_footer_size
            return self._as_uint8(col_view)[header_offset:header_offset +
                                            self._pf.packet_header_size]
        else:
            return np.empty(0, dtype=np.uint8)

    def packet_footer(self, col_view: np.ndarray) -> np.ndarray:
        if self._pf.packet_footer_size:
            footer_offset = (self._pf.col_header_size +
                             self._pf.col_footer_size +
                             self._pf.packet_header_size)
            return self._as_uint8(col_view)[footer_offset:footer_offset +
                                            self._pf.packet_footer_size]
        else:
            return np.empty(0, dtype=np.uint8)

    def col_footer(self, col_view: np.ndarray) -> np.ndarray:
        if self._pf.col_footer_size:
            return self._as_uint8(
                col_view)[self._pf.col_header_size:self._pf.col_header_size +
                          self._pf.col_footer_size]
        else:
            return np.empty(0, dtype=np.uint8)

    def col_header(self, col_view: np.ndarray) -> np.ndarray:
        if self._pf.col_header_size:
            return self._as_uint8(col_view)[:self._pf.col_header_size]
        else:
            return np.empty(0, dtype=np.uint8)

    def _as_uint8(self, col_buf: np.ndarray) -> np.ndarray:
        if not col_buf.flags['C_CONTIGUOUS']:
            col_buf_cont = np.ascontiguousarray(col_buf)
            return np.frombuffer(col_buf_cont, dtype=np.uint8)
        else:
            return np.frombuffer(col_buf, dtype=np.uint8)


def gen_scan_buffers_fast(ls: client.LidarScan,
                          info: client.SensorInfo) -> Iterator[bytearray]:
    """Reconstruct lidar packets from a LidarScan (RAW_HEADERS field required).

    `fast` version means that it's a more ugly implementation with all offsets
    caluation done in a loop. Faster 2-3x if compared with a
    ``gen_scan_buffers_nice()`` version.

    NOTE: Currently only headers and footers of the packets and headers and
    footers of the columns are put into buffers.

    Args:
        ls: LidarScan with RAW_HEADERS field. If it doesn't have RAW_HEADERS
            the result is empty []
        info: metadata of the `ls` scan

    Returns:
        A generator of lidar packets that will produce the same LidarScan if
        passed through the ScanBatcher again (less fields data).
    """

    if client.ChanField.RAW_HEADERS not in ls.fields:
        return []

    field_rh = ls.field(client.ChanField.RAW_HEADERS)
    pf = client._client.PacketFormat.from_info(info)

    buf_view_isize = field_rh.itemsize
    packet_header_size = int(pf.packet_header_size / buf_view_isize)
    col_header_size = int(pf.col_header_size / buf_view_isize)
    col_footer_size = int(pf.col_footer_size / buf_view_isize)
    col_size = int(pf.col_size / buf_view_isize)
    packet_footer_size = int(pf.packet_footer_size / buf_view_isize)

    for pi in range(0, ls.w, pf.columns_per_packet):
        col0_buf = field_rh[:, pi]

        if not np.any(col0_buf):
            continue

        buf = bytearray(pf.lidar_packet_size)
        buf_view = np.frombuffer(buf, dtype=field_rh.dtype)

        buf_view[0:packet_header_size] = col0_buf[
            col_header_size + col_footer_size:col_header_size +
            col_footer_size + packet_header_size]

        buf_view[packet_header_size + pf.columns_per_packet *
                 col_size:] = col0_buf[col_header_size + col_footer_size +
                                      packet_header_size:col_header_size +
                                      col_footer_size + packet_header_size +
                                      packet_footer_size]

        for pc in range(0, pf.columns_per_packet):
            # copy columns headers for (pi + pc) column
            col_offset = packet_header_size + pc * col_size
            buf_view[col_offset:col_offset +
                     col_header_size] = field_rh[:col_header_size,
                                                 pi + pc]
            buf_view[col_offset + col_size - col_footer_size:col_offset +
                     col_size] = field_rh[col_header_size:col_header_size +
                                          col_footer_size,
                                          pi + pc]
        yield buf


def gen_scan_buffers_nice(ls: client.LidarScan,
                          info: client.SensorInfo) -> Iterator[bytearray]:
    """Reconstruct lidar packets from a LidarScan (RAW_HEADERS field required).

    `nice` version means that it's a more structured accessors that are
    easy to use and reason about, but slower in execution (2-3x slower).

    NOTE: Currently only headers and footers of the packets and headers and
    footers of the columns are put into buffers.

    Args:
        ls: LidarScan with RAW_HEADERS field. If it doesn't have RAW_HEADERS
            the result is empty []
        info: metadata of the `ls` scan

    Returns:
        A generator of lidar packets that will produce the same LidarScan if
        passed through the ScanBatcher again (less fields data).
    """

    if client.ChanField.RAW_HEADERS not in ls.fields:
        return

    field_rh = ls.field(client.ChanField.RAW_HEADERS)
    pf = client._client.PacketFormat.from_info(info)

    lph = LidarPacketHeaders(pf)
    rhf = RawHeadersFormat(pf)

    # iteraring by packet index (pi) in lidar scan columns
    for pi in range(0, ls.w, pf.columns_per_packet):
        if not np.any(field_rh[:, pi]):
            continue
        buf = bytearray(pf.lidar_packet_size)
        lph.packet_header(buf)[:] = rhf.packet_header(field_rh[:, pi])
        lph.packet_footer(buf)[:] = rhf.packet_footer(field_rh[:, pi])

        for pc in range(0, pf.columns_per_packet):
            # copy columns headers: pi + pc
            lph.col_header(buf, pc)[:] = rhf.col_header(field_rh[:, pi + pc])
            lph.col_footer(buf, pc)[:] = rhf.col_footer(field_rh[:, pi + pc])

        yield buf


def gen_scan_buffers(ls: client.LidarScan,
                     info: client.SensorInfo) -> Iterator[bytearray]:
    """Reconstruct lidar packets from a LidarScan (RAW_HEADERS field required).

    NOTE: Currently only headers and footers of the packets and headers and
    footers of the columns are put into buffers.

    """
    return gen_scan_buffers_fast(ls, info)


def scan_to_buffers(ls: client.LidarScan,
                    info: client.SensorInfo) -> List[bytearray]:
    """Converts LidarScar to a lidar_packet buffers (only headers data inside)

    NOTE: Currently only headers and footers of the packets and headers and
    footers of the columns are put into buffers.

    Args:
        ls: LidarScan with RAW_HEADERS field. If it doesn't have RAW_HEADERS
            the result is empty []
        info: metadata of the `ls` scan

    Returns:
        A set of lidar packets that will produce the same LidarScan if passed
        through the ScanBatcher again (less fields data)
    """
    return list(gen_scan_buffers(ls, info))


def terminator_buffer(info: client.SensorInfo,
                      last_buf: client.BufferT) -> bytearray:
    """Makes a next after the last lidar packet buffer that finishes LidarScan.

    Main mechanism is to set the next frame_id (``frame_id + 1``) in uint16
    format of the lidar packet with some arbitrary data (filled with ``0xfe``).

    Such a next after the last lidar packet is needed for the ScanBatcher to
    correctly finish the scan (i.e. zero out column fields that are not
    arrived which is critical if used in a way when LidarScan object is reused.)

    NOTE[pb]: in Python it's almost always the new LidarScan is created from
              scretch and used as a receiver of lidar packet in the batching
              implmentation, thus finalization with zeros and a proper cut can
              be skipped, however it's a huge difference from C++ batching loop
              impl and it's important to keep things closer to C++ and also have
              a normal way to cut the very last LidarScan in a streams.

    Args:
        info: metadata of the current batcher that is in use
        last_buf: the last buffer that was passed to batcher.

    """

    # get frame_id using client.PacketFormat
    pf = client._client.PacketFormat.from_info(info)
    curr_fid = pf.frame_id(last_buf)

    pformat = PacketFormat.from_metadata(info)
    last_buf_view = np.frombuffer(last_buf,
                                  dtype=np.uint8,
                                  count=pf.lidar_packet_size)

    # get frame_id using parsing.py PacketFormat and compare with client result
    assert pformat.frame_id(last_buf_view) == curr_fid, "_client.PacketFormat " \
        "and parsing.py PacketFormat should get the same frame_id value from buffer"

    # making a dummy data for the terminal lidar_packet
    tbuf = bytearray([0xfe for _ in range(pf.lidar_packet_size)])
    tbuf_view = np.frombuffer(tbuf, dtype=np.uint8, count=pf.lidar_packet_size)

    # update the frame_id so it causes the LidarScan finishing routine
    # NOTE: frame_id is uint16 datatype so we need to properly wrap it on +1
    pformat.set_frame_id(tbuf_view, (curr_fid + 1) % 0xffff)

    return tbuf


def buffers_to_scan(
        lidar_bufs: List[client.BufferT],
        info: client.SensorInfo,
        fields: Optional[Dict[ChanField,
                              FieldDType]] = None) -> client.LidarScan:
    """Batch buffers that belongs to a single scan into a LidarScan object.

    Errors if lidar_bufs buffers do not belong to a single LidarScan. Typically
    incosistent measurement_ids or frame_ids in buffers is an error, as well
    as more buffers then a single LidarScan of a specified PacketFormat can take.
    """
    w = info.format.columns_per_frame
    h = info.format.pixels_per_column
    _fields = fields if fields is not None else default_scan_fields(
        info.format.udp_profile_lidar)
    ls = client._client.LidarScan(h, w, _fields)
    pf = client._client.PacketFormat.from_info(info)
    batch = client._client.ScanBatcher(w, pf)
    for idx, buf in enumerate(lidar_bufs):
        assert not batch(buf, ls), "lidar_bufs buffers should belong to a " \
            f"single LidarScan, but {idx} of {len(lidar_bufs)} buffers already " \
            "cut a LidarScan"

    if lidar_bufs:
        assert batch(terminator_buffer(info, lidar_bufs[-1]),
                     ls), "Terminator buffer should cause a cut of LidarScan"

    # if all expected lidar buffers constraints are satisfied we have a batched
    # lidar scan at the end
    return ls
