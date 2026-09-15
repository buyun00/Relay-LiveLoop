#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace RelayLiveLoop
{
    public sealed class RuntimePlayerTransportOptions
    {
        public RuntimePlayerTransportOptions(
            string hostAddress = "127.0.0.1",
            int port = 18761,
            TimeSpan? connectTimeout = null,
            TimeSpan? frameTimeout = null,
            TimeSpan? requestWaitTimeout = null,
            TimeSpan? reconnectMinimumDelay = null,
            TimeSpan? reconnectMaximumDelay = null,
            TimeSpan? shutdownTimeout = null)
        {
            IPAddress parsed;
            if (!IPAddress.TryParse(hostAddress, out parsed) || !IPAddress.IsLoopback(parsed))
            {
                throw new ArgumentException("The Player transport address must be a numeric loopback address.", nameof(hostAddress));
            }

            if (port <= 0 || port > 65535) throw new ArgumentOutOfRangeException(nameof(port));
            HostAddress = parsed;
            Port = port;
            ConnectTimeout = ValidateTimeout(connectTimeout ?? TimeSpan.FromSeconds(5), nameof(connectTimeout));
            FrameTimeout = ValidateTimeout(frameTimeout ?? TimeSpan.FromMinutes(5), nameof(frameTimeout));
            RequestWaitTimeout = ValidateTimeout(requestWaitTimeout ?? TimeSpan.FromSeconds(30), nameof(requestWaitTimeout));
            ReconnectMinimumDelay = ValidateReconnectDelay(
                reconnectMinimumDelay ?? TimeSpan.FromMilliseconds(100),
                nameof(reconnectMinimumDelay));
            ReconnectMaximumDelay = ValidateReconnectDelay(
                reconnectMaximumDelay ?? TimeSpan.FromSeconds(5),
                nameof(reconnectMaximumDelay));
            if (ReconnectMaximumDelay < ReconnectMinimumDelay)
            {
                throw new ArgumentException("Maximum reconnect delay must not be shorter than the minimum delay.");
            }

            ShutdownTimeout = ValidateTimeout(shutdownTimeout ?? TimeSpan.FromSeconds(5), nameof(shutdownTimeout));
        }

        public IPAddress HostAddress { get; private set; }
        public int Port { get; private set; }
        public TimeSpan ConnectTimeout { get; private set; }
        public TimeSpan FrameTimeout { get; private set; }
        public TimeSpan RequestWaitTimeout { get; private set; }
        public TimeSpan ReconnectMinimumDelay { get; private set; }
        public TimeSpan ReconnectMaximumDelay { get; private set; }
        public TimeSpan ShutdownTimeout { get; private set; }

        private static TimeSpan ValidateTimeout(TimeSpan value, string name)
        {
            if (value <= TimeSpan.Zero || value > TimeSpan.FromMinutes(5))
            {
                throw new ArgumentOutOfRangeException(name);
            }

            return value;
        }

        private static TimeSpan ValidateReconnectDelay(TimeSpan value, string name)
        {
            if (value <= TimeSpan.Zero || value > TimeSpan.FromMinutes(1))
            {
                throw new ArgumentOutOfRangeException(name);
            }

            return value;
        }
    }

    /// <summary>
    /// Development-only Player endpoint. It actively connects to the local Python Host, then
    /// serves authenticated Host commands on that accepted connection. Reconnection establishes
    /// a new authenticated connection only; the Player never queues or replays commands.
    /// </summary>
    public sealed class LoopbackRuntimePlayerTransport : IDisposable
    {
        private readonly object _sync = new object();
        private readonly RuntimeBridgeCore _bridge;
        private readonly RuntimeTransportCommandAdapter _commands;
        private readonly RuntimePlayerTransportOptions _options;
        private CancellationTokenSource _stopping;
        private Task _runTask;
        private TcpClient _activeClient;
        private bool _started;
        private bool _disposed;
        private bool _authenticated;

        public LoopbackRuntimePlayerTransport(
            RuntimeBridgeCore bridge,
            IRuntimeTransportCommandHandler handler,
            RuntimePlayerTransportOptions options = null)
            : this(bridge, handler, null, options)
        {
        }

        public LoopbackRuntimePlayerTransport(
            RuntimeBridgeCore bridge,
            IRuntimeTransportCommandHandler handler,
            IRuntimeTransportAsyncCommandHandler asyncHandler,
            RuntimePlayerTransportOptions options = null)
        {
            _bridge = bridge ?? throw new ArgumentNullException(nameof(bridge));
            _commands = asyncHandler == null
                ? new RuntimeTransportCommandAdapter(bridge, handler)
                : new RuntimeTransportCommandAdapter(bridge, handler, asyncHandler);
            _options = options ?? new RuntimePlayerTransportOptions();
        }

        /// <summary>Connectivity only. This never means Hotfix, Reload, or any provider is verified.</summary>
        public bool IsAuthenticated
        {
            get
            {
                lock (_sync) return _authenticated;
            }
        }

        public void Start()
        {
            lock (_sync)
            {
                ThrowIfDisposed();
                if (_started) throw new InvalidOperationException("Player transport has already started.");
                _started = true;
                _stopping = new CancellationTokenSource();
                _runTask = RunReconnectLoopAsync(_stopping.Token);
            }
        }

        public async Task StopAsync()
        {
            Task runTask;
            CancellationTokenSource stopping;
            BeginStop();
            lock (_sync)
            {
                runTask = _runTask;
                stopping = _stopping;
            }
            if (runTask == null) return;
            var completed = await Task.WhenAny(
                runTask,
                Task.Delay(_options.ShutdownTimeout)).ConfigureAwait(false);
            if (!ReferenceEquals(completed, runTask))
            {
                throw new TimeoutException("Player transport did not stop within the configured shutdown timeout.");
            }

            await runTask.ConfigureAwait(false);
            lock (_sync)
            {
                if (ReferenceEquals(_stopping, stopping)) _stopping = null;
            }
            if (stopping != null) stopping.Dispose();
        }

        public void Dispose()
        {
            lock (_sync)
            {
                if (_disposed) return;
            }

            BeginStop();
            lock (_sync)
            {
                if (_disposed) return;
                _disposed = true;
            }
        }

        private async Task RunReconnectLoopAsync(CancellationToken cancellationToken)
        {
            var reconnectDelay = _options.ReconnectMinimumDelay;
            while (!cancellationToken.IsCancellationRequested)
            {
                TcpClient client = null;
                try
                {
                    client = new TcpClient(_options.HostAddress.AddressFamily) { NoDelay = true };
                    lock (_sync)
                    {
                        if (cancellationToken.IsCancellationRequested)
                        {
                            client.Close();
                            break;
                        }

                        _activeClient = client;
                    }

                    await ConnectWithTimeoutAsync(client, cancellationToken).ConfigureAwait(false);
                    reconnectDelay = _options.ReconnectMinimumDelay;
                    await ServeConnectionAsync(client, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    break;
                }
                catch (SocketException)
                {
                    // Expected while the local Host is absent or an accepted connection is lost.
                }
                catch (IOException)
                {
                    // Expected when the peer closes or a bounded frame cannot be completed.
                }
                catch (RelayLiveLoopProtocolException)
                {
                    // The connection is discarded after a bounded protocol failure.
                }
                catch (SerializationException)
                {
                    // Malformed JSON is connection-scoped and never reaches the main thread.
                }
                finally
                {
                    lock (_sync)
                    {
                        _authenticated = false;
                        if (ReferenceEquals(_activeClient, client)) _activeClient = null;
                    }

                    if (client != null) client.Close();
                }

                if (cancellationToken.IsCancellationRequested) break;
                try
                {
                    await Task.Delay(reconnectDelay, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    break;
                }
                var doubled = TimeSpan.FromMilliseconds(reconnectDelay.TotalMilliseconds * 2);
                reconnectDelay = doubled <= _options.ReconnectMaximumDelay
                    ? doubled
                    : _options.ReconnectMaximumDelay;
            }
        }

        private async Task ConnectWithTimeoutAsync(TcpClient client, CancellationToken cancellationToken)
        {
            var connectTask = client.ConnectAsync(_options.HostAddress, _options.Port);
            var delayTask = Task.Delay(_options.ConnectTimeout, cancellationToken);
            var completed = await Task.WhenAny(connectTask, delayTask).ConfigureAwait(false);
            if (!ReferenceEquals(completed, connectTask))
            {
                cancellationToken.ThrowIfCancellationRequested();
                client.Close();
                throw new RelayLiveLoopProtocolException(
                    RelayLiveLoopErrorCode.Timeout,
                    "Timed out connecting to the local Host listener.");
            }

            await connectTask.ConfigureAwait(false);
        }

        private async Task ServeConnectionAsync(TcpClient client, CancellationToken cancellationToken)
        {
            var stream = client.GetStream();
            string authenticatedConnectionId = null;
            string issuedChallengeId = null;
            try
            {
                await WriteMessageAsync(stream, RuntimeTransportWireMessage.PlayerHello(_bridge.Identity), cancellationToken)
                    .ConfigureAwait(false);
                while (!cancellationToken.IsCancellationRequested)
                {
                    RuntimeTransportWireMessage message;
                    try
                    {
                        var bytes = await _bridge.Framer.ReadAsync(
                            stream,
                            _options.FrameTimeout,
                            cancellationToken).ConfigureAwait(false);
                        message = RuntimeTransportWireCodec.Deserialize(bytes);
                    }
                    catch (RelayLiveLoopProtocolException exception)
                    {
                        await TryWriteErrorAsync(
                            stream,
                            null,
                            RelayLiveLoopErrors.Create(
                                exception.Code,
                                "runtime_transport_frame",
                                exception.Message,
                                false),
                            cancellationToken).ConfigureAwait(false);
                        throw;
                    }
                    catch (SerializationException exception)
                    {
                        await TryWriteErrorAsync(
                            stream,
                            null,
                            RelayLiveLoopErrors.Create(
                                RelayLiveLoopErrorCode.InvalidMessage,
                                "runtime_transport_decode",
                                exception.Message,
                                false),
                            cancellationToken).ConfigureAwait(false);
                        throw;
                    }

                    if (string.Equals(message.Kind, "handshake.begin", StringComparison.Ordinal))
                    {
                        if (issuedChallengeId != null || authenticatedConnectionId != null)
                        {
                            await WriteErrorAsync(stream, null, InvalidState("Handshake was already started."), cancellationToken)
                                .ConfigureAwait(false);
                            throw new SerializationException("Repeated handshake.begin.");
                        }

                        var begun = _bridge.Authentication.BeginHandshake(
                            message.SessionId,
                            message.LaunchId,
                            message.ExpectedRuntimeRevision,
                            message.ProtocolVersion,
                            message.ClientNonce);
                        if (!begun.Succeeded)
                        {
                            await WriteErrorAsync(stream, null, begun.Error, cancellationToken).ConfigureAwait(false);
                            return;
                        }

                        issuedChallengeId = begun.Value.ChallengeId;
                        await WriteMessageAsync(
                            stream,
                            RuntimeTransportWireMessage.HandshakeChallenge(begun.Value),
                            cancellationToken).ConfigureAwait(false);
                        continue;
                    }

                    if (string.Equals(message.Kind, "handshake.complete", StringComparison.Ordinal))
                    {
                        if (issuedChallengeId == null || authenticatedConnectionId != null ||
                            !string.Equals(issuedChallengeId, message.ChallengeId, StringComparison.Ordinal))
                        {
                            await WriteErrorAsync(stream, null, InvalidState("Handshake challenge does not belong to this socket."), cancellationToken)
                                .ConfigureAwait(false);
                            return;
                        }

                        var completed = _bridge.Authentication.CompleteHandshake(
                            message.ChallengeId,
                            message.Proof);
                        issuedChallengeId = null;
                        if (!completed.Succeeded)
                        {
                            await WriteErrorAsync(stream, null, completed.Error, cancellationToken).ConfigureAwait(false);
                            return;
                        }

                        authenticatedConnectionId = completed.Value.ConnectionId;
                        lock (_sync) _authenticated = true;
                        await WriteMessageAsync(
                            stream,
                            RuntimeTransportWireMessage.HandshakeCompleted(completed.Value),
                            cancellationToken).ConfigureAwait(false);
                        continue;
                    }

                    if (string.Equals(message.Kind, "request", StringComparison.Ordinal))
                    {
                        if (authenticatedConnectionId == null ||
                            !string.Equals(authenticatedConnectionId, message.ConnectionId, StringComparison.Ordinal))
                        {
                            await WriteErrorAsync(
                                stream,
                                message.RequestId,
                                RelayLiveLoopErrors.Create(
                                    RelayLiveLoopErrorCode.AuthRequired,
                                    "runtime_transport_authenticate",
                                    "The request does not belong to this authenticated socket.",
                                    false),
                                cancellationToken).ConfigureAwait(false);
                            return;
                        }

                        await HandleRequestAsync(stream, message, cancellationToken).ConfigureAwait(false);
                        continue;
                    }

                    await WriteErrorAsync(stream, message.RequestId, InvalidState("Wire message kind is invalid for this connection state."), cancellationToken)
                        .ConfigureAwait(false);
                    return;
                }
            }
            finally
            {
                lock (_sync) _authenticated = false;
                if (authenticatedConnectionId != null)
                {
                    _bridge.Authentication.CloseConnection(authenticatedConnectionId);
                }
            }
        }

        private async Task HandleRequestAsync(
            NetworkStream stream,
            RuntimeTransportWireMessage message,
            CancellationToken transportCancellation)
        {
            byte[] payload;
            try
            {
                payload = RuntimeTransportWireCodec.DecodeCanonicalBase64(message.PayloadBase64);
            }
            catch (FormatException exception)
            {
                await WriteErrorAsync(
                    stream,
                    message.RequestId,
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.InvalidMessage,
                        "runtime_transport_decode",
                        exception.Message,
                        false),
                    transportCancellation).ConfigureAwait(false);
                return;
            }

            var authentication = new RequestAuthentication(
                message.ConnectionId,
                message.Sequence,
                message.SentAtUnixMilliseconds,
                message.RequestId,
                message.Operation,
                message.PayloadSha256,
                message.Proof);
            var request = new AuthenticatedRuntimeRequest(
                message.SessionId,
                message.ExpectedRuntimeRevision,
                authentication,
                payload);
            var task = _commands.ScheduleAsync(request, transportCancellation);
            var timeout = Task.Delay(_options.RequestWaitTimeout, transportCancellation);
            var completed = await Task.WhenAny(task, timeout).ConfigureAwait(false);
            if (!ReferenceEquals(completed, task))
            {
                transportCancellation.ThrowIfCancellationRequested();
                var details = new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    { "requestId", message.RequestId ?? string.Empty },
                    { "dispatchMayHaveStarted", "true" },
                    { "automaticReplayAllowed", "false" }
                };
                await WriteErrorAsync(
                    stream,
                    message.RequestId,
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StateUnknown,
                        "runtime_transport_wait",
                        "Timed out after dispatch; the runtime outcome is unknown and the request was not replayed.",
                        true,
                        null,
                        details),
                    transportCancellation).ConfigureAwait(false);
                return;
            }

            RelayLiveLoopResult result;
            try
            {
                result = await task.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                var details = new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    { "requestId", message.RequestId ?? string.Empty },
                    { "dispatchMayHaveStarted", "true" },
                    { "automaticReplayAllowed", "false" }
                };
                await WriteErrorAsync(
                    stream,
                    message.RequestId,
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StateUnknown,
                        "runtime_transport_wait",
                        "Dispatch was cancelled without a terminal runtime result.",
                        true,
                        null,
                        details),
                    transportCancellation).ConfigureAwait(false);
                return;
            }

            if (!result.Succeeded)
            {
                await WriteErrorAsync(stream, message.RequestId, result.Error, transportCancellation)
                    .ConfigureAwait(false);
                return;
            }

            var execution = result as RuntimeTransportExecutionResult;
            if (execution == null || execution.Payload == null)
            {
                await WriteErrorAsync(
                    stream,
                    message.RequestId,
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        "runtime_transport_result",
                        "The shared bridge task completed without a transport payload.",
                        false),
                    transportCancellation).ConfigureAwait(false);
                return;
            }

            await WriteMessageAsync(
                stream,
                RuntimeTransportWireMessage.Response(message.RequestId, execution),
                transportCancellation).ConfigureAwait(false);
        }

        private Task WriteErrorAsync(
            NetworkStream stream,
            string requestId,
            RelayLiveLoopError error,
            CancellationToken cancellationToken)
        {
            return WriteMessageAsync(
                stream,
                RuntimeTransportWireMessage.ErrorResponse(requestId, error),
                cancellationToken);
        }

        private async Task TryWriteErrorAsync(
            NetworkStream stream,
            string requestId,
            RelayLiveLoopError error,
            CancellationToken cancellationToken)
        {
            try
            {
                await WriteErrorAsync(stream, requestId, error, cancellationToken).ConfigureAwait(false);
            }
            catch (Exception exception) when (
                exception is IOException ||
                exception is SocketException ||
                exception is ObjectDisposedException ||
                exception is RelayLiveLoopProtocolException)
            {
                // A peer that cannot receive the bounded protocol error is simply disconnected.
            }
        }

        private Task WriteMessageAsync(
            NetworkStream stream,
            RuntimeTransportWireMessage message,
            CancellationToken cancellationToken)
        {
            return _bridge.Framer.WriteAsync(
                stream,
                RuntimeTransportWireCodec.Serialize(message),
                _options.FrameTimeout,
                cancellationToken);
        }

        private static RelayLiveLoopError InvalidState(string message)
        {
            return RelayLiveLoopErrors.Create(
                RelayLiveLoopErrorCode.InvalidMessage,
                "runtime_transport_protocol",
                message,
                false);
        }

        private void BeginStop()
        {
            lock (_sync)
            {
                if (_stopping != null && !_stopping.IsCancellationRequested) _stopping.Cancel();
                if (_activeClient != null) _activeClient.Close();
                _authenticated = false;
            }
        }

        private void ThrowIfDisposed()
        {
            if (_disposed) throw new ObjectDisposedException(nameof(LoopbackRuntimePlayerTransport));
        }
    }

    [DataContract]
    internal sealed class RuntimeTransportWireDetail
    {
        [DataMember(Name = "key", EmitDefaultValue = false)] public string Key;
        [DataMember(Name = "value", EmitDefaultValue = false)] public string Value;
    }

    [DataContract]
    internal sealed class RuntimeTransportWireError
    {
        [DataMember(Name = "code", EmitDefaultValue = false)] public string Code;
        [DataMember(Name = "stage", EmitDefaultValue = false)] public string Stage;
        [DataMember(Name = "message", EmitDefaultValue = false)] public string Message;
        [DataMember(Name = "recoverable")] public bool Recoverable;
        [DataMember(Name = "runtimeChangedKnown")] public bool RuntimeChangedKnown;
        [DataMember(Name = "runtimeChanged")] public bool RuntimeChanged;
        [DataMember(Name = "details", EmitDefaultValue = false)] public RuntimeTransportWireDetail[] Details;
    }

    // These fields are populated reflectively by DataContractJsonSerializer for inbound Host
    // messages, so the compiler cannot observe their assignments.
#pragma warning disable 0649
    [DataContract]
    internal sealed class RuntimeTransportWireMessage
    {
        [DataMember(Name = "kind", EmitDefaultValue = false)] public string Kind;
        [DataMember(Name = "protocolVersion", EmitDefaultValue = false)] public int ProtocolVersion;
        [DataMember(Name = "sessionId", EmitDefaultValue = false)] public string SessionId;
        [DataMember(Name = "launchId", EmitDefaultValue = false)] public string LaunchId;
        [DataMember(Name = "runtimeRevision", EmitDefaultValue = false)] public string RuntimeRevision;
        [DataMember(Name = "expectedRuntimeRevision", EmitDefaultValue = false)] public string ExpectedRuntimeRevision;
        [DataMember(Name = "clientNonce", EmitDefaultValue = false)] public string ClientNonce;
        [DataMember(Name = "serverNonce", EmitDefaultValue = false)] public string ServerNonce;
        [DataMember(Name = "challengeId", EmitDefaultValue = false)] public string ChallengeId;
        [DataMember(Name = "connectionId", EmitDefaultValue = false)] public string ConnectionId;
        [DataMember(Name = "expiresAtUnixMilliseconds", EmitDefaultValue = false)] public long ExpiresAtUnixMilliseconds;
        [DataMember(Name = "sequence", EmitDefaultValue = false)] public long Sequence;
        [DataMember(Name = "sentAtUnixMilliseconds", EmitDefaultValue = false)] public long SentAtUnixMilliseconds;
        [DataMember(Name = "requestId", EmitDefaultValue = false)] public string RequestId;
        [DataMember(Name = "operation", EmitDefaultValue = false)] public string Operation;
        [DataMember(Name = "payloadSha256", EmitDefaultValue = false)] public string PayloadSha256;
        [DataMember(Name = "proof", EmitDefaultValue = false)] public string Proof;
        [DataMember(Name = "payloadBase64", EmitDefaultValue = false)] public string PayloadBase64;
        [DataMember(Name = "schemaId", EmitDefaultValue = false)] public string SchemaId;
        [DataMember(Name = "schemaVersion", EmitDefaultValue = false)] public int SchemaVersion;
        [DataMember(Name = "mediaType", EmitDefaultValue = false)] public string MediaType;
        [DataMember(Name = "runtimeChangedKnown", EmitDefaultValue = false)] public bool? RuntimeChangedKnown;
        [DataMember(Name = "runtimeChanged", EmitDefaultValue = false)] public bool? RuntimeChanged;
        [DataMember(Name = "error", EmitDefaultValue = false)] public RuntimeTransportWireError Error;

        public static RuntimeTransportWireMessage PlayerHello(RuntimeSessionIdentity identity)
        {
            return new RuntimeTransportWireMessage
            {
                Kind = "player.hello",
                ProtocolVersion = identity.ProtocolVersion,
                SessionId = identity.SessionId,
                LaunchId = identity.LaunchId,
                RuntimeRevision = identity.RuntimeRevision
            };
        }

        public static RuntimeTransportWireMessage HandshakeChallenge(HandshakeChallenge challenge)
        {
            return new RuntimeTransportWireMessage
            {
                Kind = "handshake.challenge",
                ChallengeId = challenge.ChallengeId,
                ClientNonce = challenge.ClientNonce,
                ServerNonce = challenge.ServerNonce,
                RuntimeRevision = challenge.RuntimeRevision,
                ExpiresAtUnixMilliseconds = challenge.ExpiresAtUtc.ToUnixTimeMilliseconds()
            };
        }

        public static RuntimeTransportWireMessage HandshakeCompleted(AuthenticatedConnection connection)
        {
            return new RuntimeTransportWireMessage
            {
                Kind = "handshake.completed",
                ConnectionId = connection.ConnectionId,
                ExpiresAtUnixMilliseconds = connection.ExpiresAtUtc.ToUnixTimeMilliseconds()
            };
        }

        public static RuntimeTransportWireMessage Response(
            string requestId,
            RuntimeTransportExecutionResult result)
        {
            var payload = result.Payload;
            return new RuntimeTransportWireMessage
            {
                Kind = "response",
                RequestId = requestId,
                RuntimeRevision = result.RuntimeRevisionAfter,
                RuntimeChangedKnown = true,
                RuntimeChanged = result.RuntimeChanged,
                SchemaId = payload.SchemaId,
                SchemaVersion = payload.SchemaVersion,
                MediaType = payload.MediaType,
                PayloadBase64 = Convert.ToBase64String(payload.Bytes)
            };
        }

        public static RuntimeTransportWireMessage ErrorResponse(string requestId, RelayLiveLoopError error)
        {
            return new RuntimeTransportWireMessage
            {
                Kind = "error",
                RequestId = requestId,
                RuntimeChangedKnown = error.RuntimeChanged.HasValue,
                RuntimeChanged = error.RuntimeChanged == true,
                Error = RuntimeTransportWireErrors.FromCore(error)
            };
        }
    }
#pragma warning restore 0649

    internal static class RuntimeTransportWireErrors
    {
        public static RuntimeTransportWireError FromCore(RelayLiveLoopError error)
        {
            if (error == null) throw new ArgumentNullException(nameof(error));
            var details = new List<RuntimeTransportWireDetail>();
            foreach (var pair in error.Details)
            {
                details.Add(new RuntimeTransportWireDetail { Key = pair.Key, Value = pair.Value });
            }

            var code = ToHostCode(error.Code);
            var transportCode = error.Code.ToString();
            if (!string.Equals(code.Replace("_", string.Empty), transportCode, StringComparison.OrdinalIgnoreCase))
            {
                details.Add(new RuntimeTransportWireDetail { Key = "transportCode", Value = transportCode });
            }

            return new RuntimeTransportWireError
            {
                Code = code,
                Stage = error.Stage,
                Message = error.Message,
                Recoverable = error.Recoverable,
                RuntimeChangedKnown = error.RuntimeChanged.HasValue,
                RuntimeChanged = error.RuntimeChanged == true,
                Details = details.ToArray()
            };
        }

        private static string ToHostCode(RelayLiveLoopErrorCode code)
        {
            switch (code)
            {
                case RelayLiveLoopErrorCode.CapabilityUnavailable: return "CAPABILITY_UNAVAILABLE";
                case RelayLiveLoopErrorCode.AuthRequired: return "AUTH_REQUIRED";
                case RelayLiveLoopErrorCode.ContractMismatch: return "CONTRACT_MISMATCH";
                case RelayLiveLoopErrorCode.WrongSession: return "WRONG_SESSION";
                case RelayLiveLoopErrorCode.StaleTarget: return "STALE_TARGET";
                case RelayLiveLoopErrorCode.InputChanged: return "INPUT_CHANGED";
                case RelayLiveLoopErrorCode.CompileFailed: return "COMPILE_FAILED";
                case RelayLiveLoopErrorCode.ResourceBuildFailed: return "RESOURCE_BUILD_FAILED";
                case RelayLiveLoopErrorCode.ApprovalRequired: return "APPROVAL_REQUIRED";
                case RelayLiveLoopErrorCode.UnloadRefused: return "UNLOAD_REFUSED";
                case RelayLiveLoopErrorCode.RestoreFailed: return "RESTORE_FAILED";
                case RelayLiveLoopErrorCode.StateUnknown: return "STATE_UNKNOWN";
                case RelayLiveLoopErrorCode.InvalidMessage:
                case RelayLiveLoopErrorCode.MessageTooLarge:
                    return "INVALID_REQUEST";
                case RelayLiveLoopErrorCode.Timeout: return "STATE_UNKNOWN";
                case RelayLiveLoopErrorCode.Busy: return "CONFLICT";
                default: return "INTERNAL_ERROR";
            }
        }
    }

    internal static class RuntimeTransportWireCodec
    {
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);

        public static byte[] Serialize(RuntimeTransportWireMessage message)
        {
            if (message == null) throw new ArgumentNullException(nameof(message));
            using (var stream = new MemoryStream())
            {
                Serializer().WriteObject(stream, message);
                return stream.ToArray();
            }
        }

        public static RuntimeTransportWireMessage Deserialize(byte[] bytes)
        {
            if (bytes == null || bytes.Length == 0)
            {
                throw new SerializationException("Wire JSON must not be empty.");
            }

            try
            {
                StrictUtf8.GetString(bytes);
                using (var stream = new MemoryStream(bytes, false))
                {
                    var message = Serializer().ReadObject(stream) as RuntimeTransportWireMessage;
                    if (message == null || string.IsNullOrWhiteSpace(message.Kind))
                    {
                        throw new SerializationException("Wire JSON requires a non-empty kind.");
                    }

                    return message;
                }
            }
            catch (DecoderFallbackException exception)
            {
                throw new SerializationException("Wire JSON is not valid UTF-8.", exception);
            }
            catch (SerializationException)
            {
                throw;
            }
            catch (Exception exception)
            {
                throw new SerializationException("Wire JSON is malformed.", exception);
            }
        }

        public static byte[] DecodeCanonicalBase64(string encoded)
        {
            if (string.IsNullOrEmpty(encoded)) throw new FormatException("payloadBase64 is required.");
            var bytes = Convert.FromBase64String(encoded);
            if (!string.Equals(Convert.ToBase64String(bytes), encoded, StringComparison.Ordinal))
            {
                throw new FormatException("payloadBase64 must use canonical padded Base64 without whitespace.");
            }

            return bytes;
        }

        private static DataContractJsonSerializer Serializer()
        {
            return new DataContractJsonSerializer(typeof(RuntimeTransportWireMessage));
        }
    }
}
#endif

