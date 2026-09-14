#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.IO;
using System.Net;
using System.Threading;
using System.Threading.Tasks;

namespace RelayLiveLoop
{
    public sealed class RelayLiveLoopProtocolException : Exception
    {
        public RelayLiveLoopProtocolException(RelayLiveLoopErrorCode code, string message)
            : base(message)
        {
            Code = code;
        }

        public RelayLiveLoopErrorCode Code { get; private set; }
    }

    public sealed class BoundedMessageFramer
    {
        private readonly int _maximumMessageBytes;

        public BoundedMessageFramer(int maximumMessageBytes)
        {
            if (maximumMessageBytes < 256) throw new ArgumentOutOfRangeException(nameof(maximumMessageBytes));
            _maximumMessageBytes = maximumMessageBytes;
        }

        public int MaximumMessageBytes { get { return _maximumMessageBytes; } }

        public async Task<byte[]> ReadAsync(Stream stream, TimeSpan timeout, CancellationToken cancellationToken)
        {
            if (stream == null) throw new ArgumentNullException(nameof(stream));
            if (!stream.CanRead) throw new ArgumentException("The stream is not readable.", nameof(stream));
            ValidateTimeout(timeout);

            using (var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
            {
                timeoutSource.CancelAfter(timeout);
                try
                {
                    var header = new byte[4];
                    await ReadExactlyAsync(stream, header, 0, header.Length, timeoutSource.Token).ConfigureAwait(false);
                    var networkLength = BitConverter.ToInt32(header, 0);
                    var length = IPAddress.NetworkToHostOrder(networkLength);
                    if (length <= 0)
                    {
                        throw new RelayLiveLoopProtocolException(
                            RelayLiveLoopErrorCode.InvalidMessage,
                            "Frame length must be positive.");
                    }

                    if (length > _maximumMessageBytes)
                    {
                        throw new RelayLiveLoopProtocolException(
                            RelayLiveLoopErrorCode.MessageTooLarge,
                            "Frame exceeds the configured message limit.");
                    }

                    var payload = new byte[length];
                    await ReadExactlyAsync(stream, payload, 0, payload.Length, timeoutSource.Token).ConfigureAwait(false);
                    return payload;
                }
                catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
                {
                    throw new RelayLiveLoopProtocolException(RelayLiveLoopErrorCode.Timeout, "Frame read timed out.");
                }
            }
        }

        public async Task WriteAsync(Stream stream, byte[] payload, TimeSpan timeout, CancellationToken cancellationToken)
        {
            if (stream == null) throw new ArgumentNullException(nameof(stream));
            if (!stream.CanWrite) throw new ArgumentException("The stream is not writable.", nameof(stream));
            if (payload == null) throw new ArgumentNullException(nameof(payload));
            if (payload.Length <= 0)
            {
                throw new RelayLiveLoopProtocolException(RelayLiveLoopErrorCode.InvalidMessage, "Frame payload is empty.");
            }

            if (payload.Length > _maximumMessageBytes)
            {
                throw new RelayLiveLoopProtocolException(
                    RelayLiveLoopErrorCode.MessageTooLarge,
                    "Frame exceeds the configured message limit.");
            }

            ValidateTimeout(timeout);
            using (var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
            {
                timeoutSource.CancelAfter(timeout);
                try
                {
                    var header = BitConverter.GetBytes(IPAddress.HostToNetworkOrder(payload.Length));
                    await stream.WriteAsync(header, 0, header.Length, timeoutSource.Token).ConfigureAwait(false);
                    await stream.WriteAsync(payload, 0, payload.Length, timeoutSource.Token).ConfigureAwait(false);
                    await stream.FlushAsync(timeoutSource.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
                {
                    throw new RelayLiveLoopProtocolException(RelayLiveLoopErrorCode.Timeout, "Frame write timed out.");
                }
            }
        }

        private static async Task ReadExactlyAsync(
            Stream stream,
            byte[] buffer,
            int offset,
            int count,
            CancellationToken cancellationToken)
        {
            while (count > 0)
            {
                var read = await stream.ReadAsync(buffer, offset, count, cancellationToken).ConfigureAwait(false);
                if (read == 0)
                {
                    throw new EndOfStreamException("The stream ended inside a framed message.");
                }

                offset += read;
                count -= read;
            }
        }

        private static void ValidateTimeout(TimeSpan timeout)
        {
            if (timeout <= TimeSpan.Zero || timeout > TimeSpan.FromMinutes(5))
            {
                throw new ArgumentOutOfRangeException(nameof(timeout));
            }
        }
    }
}
#endif
