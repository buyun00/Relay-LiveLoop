#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;

namespace RelayLiveLoop
{
    internal static class ManagedPngEncoder
    {
        private static readonly byte[] Signature = { 137, 80, 78, 71, 13, 10, 26, 10 };
        private static readonly uint[] CrcTable = BuildCrcTable();

        public static byte[] EncodeRgba32(int width, int height, Color32[] pixels, bool flipVertically)
        {
            if (width <= 0 || height <= 0) throw new ArgumentOutOfRangeException("Image dimensions must be positive.");
            if (pixels == null || pixels.Length != checked(width * height))
            {
                throw new ArgumentException("Pixel count does not match image dimensions.", nameof(pixels));
            }

            var rowBytes = checked(width * 4);
            var scanlines = new byte[checked((rowBytes + 1) * height)];
            for (var outputRow = 0; outputRow < height; outputRow++)
            {
                var sourceRow = flipVertically ? height - outputRow - 1 : outputRow;
                var outputOffset = outputRow * (rowBytes + 1);
                scanlines[outputOffset] = 0;
                var sourceOffset = sourceRow * width;
                for (var column = 0; column < width; column++)
                {
                    var pixel = pixels[sourceOffset + column];
                    var destination = outputOffset + 1 + (column * 4);
                    scanlines[destination] = pixel.r;
                    scanlines[destination + 1] = pixel.g;
                    scanlines[destination + 2] = pixel.b;
                    scanlines[destination + 3] = pixel.a;
                }
            }

            var zlib = BuildStoredZlib(scanlines);
            using (var output = new MemoryStream(Signature.Length + zlib.Length + 64))
            {
                output.Write(Signature, 0, Signature.Length);
                var header = new byte[13];
                WriteBigEndian(header, 0, (uint)width);
                WriteBigEndian(header, 4, (uint)height);
                header[8] = 8;
                header[9] = 6;
                WriteChunk(output, "IHDR", header);
                WriteChunk(output, "IDAT", zlib);
                WriteChunk(output, "IEND", Array.Empty<byte>());
                return output.ToArray();
            }
        }

        private static byte[] BuildStoredZlib(byte[] bytes)
        {
            var blockCount = (bytes.Length + 65534) / 65535;
            using (var output = new MemoryStream(bytes.Length + (blockCount * 5) + 6))
            {
                output.WriteByte(0x78);
                output.WriteByte(0x01);
                var offset = 0;
                while (offset < bytes.Length)
                {
                    var length = Math.Min(65535, bytes.Length - offset);
                    var final = offset + length == bytes.Length;
                    output.WriteByte(final ? (byte)0x01 : (byte)0x00);
                    output.WriteByte((byte)(length & 0xff));
                    output.WriteByte((byte)((length >> 8) & 0xff));
                    var inverted = (~length) & 0xffff;
                    output.WriteByte((byte)(inverted & 0xff));
                    output.WriteByte((byte)((inverted >> 8) & 0xff));
                    output.Write(bytes, offset, length);
                    offset += length;
                }

                var adler = Adler32(bytes);
                WriteBigEndian(output, adler);
                return output.ToArray();
            }
        }

        private static void WriteChunk(Stream output, string type, byte[] data)
        {
            var typeBytes = System.Text.Encoding.ASCII.GetBytes(type);
            WriteBigEndian(output, (uint)data.Length);
            output.Write(typeBytes, 0, typeBytes.Length);
            output.Write(data, 0, data.Length);
            var crc = 0xffffffffu;
            crc = UpdateCrc(crc, typeBytes);
            crc = UpdateCrc(crc, data);
            WriteBigEndian(output, crc ^ 0xffffffffu);
        }

        private static uint Adler32(byte[] bytes)
        {
            const uint modulus = 65521;
            uint a = 1;
            uint b = 0;
            for (var index = 0; index < bytes.Length; index++)
            {
                a = (a + bytes[index]) % modulus;
                b = (b + a) % modulus;
            }

            return (b << 16) | a;
        }

        private static uint UpdateCrc(uint crc, byte[] bytes)
        {
            for (var index = 0; index < bytes.Length; index++)
            {
                crc = CrcTable[(crc ^ bytes[index]) & 0xff] ^ (crc >> 8);
            }

            return crc;
        }

        private static uint[] BuildCrcTable()
        {
            var table = new uint[256];
            for (uint index = 0; index < table.Length; index++)
            {
                var value = index;
                for (var bit = 0; bit < 8; bit++)
                {
                    value = (value & 1) != 0 ? 0xedb88320u ^ (value >> 1) : value >> 1;
                }

                table[index] = value;
            }

            return table;
        }

        private static void WriteBigEndian(Stream output, uint value)
        {
            var bytes = new byte[4];
            WriteBigEndian(bytes, 0, value);
            output.Write(bytes, 0, bytes.Length);
        }

        private static void WriteBigEndian(byte[] bytes, int offset, uint value)
        {
            bytes[offset] = (byte)((value >> 24) & 0xff);
            bytes[offset + 1] = (byte)((value >> 16) & 0xff);
            bytes[offset + 2] = (byte)((value >> 8) & 0xff);
            bytes[offset + 3] = (byte)(value & 0xff);
        }
    }
}
#endif
