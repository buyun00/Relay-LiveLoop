#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace RelayLiveLoop
{
    public sealed class RuntimeSessionIdentity
    {
        private readonly RuntimeRevisionClock _runtimeRevisions;

        public RuntimeSessionIdentity(
            string sessionId,
            string launchId,
            string runtimeRevision,
            int protocolVersion)
        {
            SessionId = RequireCanonicalValue(sessionId, nameof(sessionId));
            LaunchId = RequireCanonicalValue(launchId, nameof(launchId));
            _runtimeRevisions = new RuntimeRevisionClock(runtimeRevision);
            if (protocolVersion <= 0) throw new ArgumentOutOfRangeException(nameof(protocolVersion));
            ProtocolVersion = protocolVersion;
        }

        public string SessionId { get; private set; }
        public string LaunchId { get; private set; }
        public string RuntimeRevision { get { return _runtimeRevisions.CurrentRevision; } }
        public IRuntimeRevisionSource RuntimeRevisions { get { return _runtimeRevisions; } }
        public int ProtocolVersion { get; private set; }

        internal RuntimeRevisionClock BindRuntimeRevisionAuthority(
            IRelayLiveLoopMainThreadGuard mainThread)
        {
            _runtimeRevisions.BindMainThreadAuthority(mainThread);
            return _runtimeRevisions;
        }

        internal static string RequireCanonicalValue(string value, string parameterName)
        {
            if (string.IsNullOrWhiteSpace(value)) throw new ArgumentException("Value is required.", parameterName);
            if (value.Length > 256) throw new ArgumentException("Value is too long.", parameterName);
            if (value.IndexOf('\n') >= 0 || value.IndexOf('\r') >= 0)
            {
                throw new ArgumentException("Value contains a protocol separator.", parameterName);
            }

            return value;
        }
    }

    public sealed class HandshakeChallenge
    {
        internal HandshakeChallenge(
            string challengeId,
            string clientNonce,
            string serverNonce,
            string runtimeRevision,
            DateTimeOffset expiresAtUtc)
        {
            ChallengeId = challengeId;
            ClientNonce = clientNonce;
            ServerNonce = serverNonce;
            RuntimeRevision = runtimeRevision;
            ExpiresAtUtc = expiresAtUtc;
        }

        public string ChallengeId { get; private set; }
        public string ClientNonce { get; private set; }
        public string ServerNonce { get; private set; }
        public string RuntimeRevision { get; private set; }
        public DateTimeOffset ExpiresAtUtc { get; private set; }
    }

    public sealed class AuthenticatedConnection
    {
        internal AuthenticatedConnection(string connectionId, DateTimeOffset expiresAtUtc)
        {
            ConnectionId = connectionId;
            ExpiresAtUtc = expiresAtUtc;
        }

        public string ConnectionId { get; private set; }
        public DateTimeOffset ExpiresAtUtc { get; private set; }
    }

    public sealed class RequestAuthentication
    {
        public RequestAuthentication(
            string connectionId,
            long sequence,
            long sentAtUnixMilliseconds,
            string requestId,
            string operation,
            string payloadSha256,
            string proof)
        {
            ConnectionId = connectionId;
            Sequence = sequence;
            SentAtUnixMilliseconds = sentAtUnixMilliseconds;
            RequestId = requestId;
            Operation = operation;
            PayloadSha256 = payloadSha256;
            Proof = proof;
        }

        public string ConnectionId { get; private set; }
        public long Sequence { get; private set; }
        public long SentAtUnixMilliseconds { get; private set; }
        public string RequestId { get; private set; }
        public string Operation { get; private set; }
        public string PayloadSha256 { get; private set; }
        public string Proof { get; private set; }
    }

    public sealed class SessionAuthentication : IDisposable
    {
        private sealed class ConnectionState
        {
            public byte[] Key;
            public DateTimeOffset ExpiresAtUtc;
            public long LastSequence;
        }

        private readonly object _sync = new object();
        private readonly RuntimeSessionIdentity _identity;
        private readonly byte[] _sharedSecret;
        private readonly Func<DateTimeOffset> _utcNow;
        private readonly TimeSpan _challengeLifetime;
        private readonly TimeSpan _connectionLifetime;
        private readonly TimeSpan _maximumClockSkew;
        private readonly int _maximumOutstandingChallenges;
        private readonly int _maximumConnections;
        private readonly Dictionary<string, HandshakeChallenge> _challenges =
            new Dictionary<string, HandshakeChallenge>(StringComparer.Ordinal);
        private readonly Dictionary<string, ConnectionState> _connections =
            new Dictionary<string, ConnectionState>(StringComparer.Ordinal);
        private bool _disposed;

        public SessionAuthentication(
            RuntimeSessionIdentity identity,
            byte[] sharedSecret,
            Func<DateTimeOffset> utcNow = null,
            TimeSpan? challengeLifetime = null,
            TimeSpan? connectionLifetime = null,
            TimeSpan? maximumClockSkew = null,
            int maximumOutstandingChallenges = 32,
            int maximumConnections = 8)
        {
            _identity = identity ?? throw new ArgumentNullException(nameof(identity));
            if (sharedSecret == null || sharedSecret.Length < 32)
            {
                throw new ArgumentException("Authentication secret must contain at least 32 bytes.", nameof(sharedSecret));
            }

            if (maximumOutstandingChallenges <= 0) throw new ArgumentOutOfRangeException(nameof(maximumOutstandingChallenges));
            if (maximumConnections <= 0) throw new ArgumentOutOfRangeException(nameof(maximumConnections));
            _sharedSecret = (byte[])sharedSecret.Clone();
            _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
            _challengeLifetime = challengeLifetime ?? TimeSpan.FromSeconds(20);
            _connectionLifetime = connectionLifetime ?? TimeSpan.FromHours(8);
            _maximumClockSkew = maximumClockSkew ?? TimeSpan.FromSeconds(30);
            _maximumOutstandingChallenges = maximumOutstandingChallenges;
            _maximumConnections = maximumConnections;
        }

        public RelayLiveLoopResult<HandshakeChallenge> BeginHandshake(
            string expectedSessionId,
            string expectedLaunchId,
            string expectedRuntimeRevision,
            int protocolVersion,
            string clientNonce)
        {
            try
            {
                RuntimeSessionIdentity.RequireCanonicalValue(expectedSessionId, nameof(expectedSessionId));
                RuntimeSessionIdentity.RequireCanonicalValue(expectedLaunchId, nameof(expectedLaunchId));
                RuntimeSessionIdentity.RequireCanonicalValue(expectedRuntimeRevision, nameof(expectedRuntimeRevision));
                RuntimeSessionIdentity.RequireCanonicalValue(clientNonce, nameof(clientNonce));
            }
            catch (ArgumentException ex)
            {
                return RelayLiveLoopResult<HandshakeChallenge>.Failure(
                    RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.InvalidMessage, "handshake", ex.Message, false));
            }

            lock (_sync)
            {
                ThrowIfDisposed();
                var now = _utcNow();
                PruneExpired(now);
                if (protocolVersion != _identity.ProtocolVersion)
                {
                    return RelayLiveLoopResult<HandshakeChallenge>.Failure(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.ContractMismatch,
                            "handshake",
                            "Protocol version does not match the running bridge.",
                            false));
                }

                if (!string.Equals(expectedSessionId, _identity.SessionId, StringComparison.Ordinal) ||
                    !string.Equals(expectedLaunchId, _identity.LaunchId, StringComparison.Ordinal))
                {
                    return RelayLiveLoopResult<HandshakeChallenge>.Failure(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.WrongSession,
                            "handshake",
                            "Session or launch identity does not match the running Player.",
                            false));
                }

                var revision = _identity.RuntimeRevisions.CaptureIfCurrent(
                    expectedRuntimeRevision,
                    "handshake");
                if (!revision.Succeeded)
                {
                    return RelayLiveLoopResult<HandshakeChallenge>.Failure(revision.Error);
                }

                if (_challenges.Count >= _maximumOutstandingChallenges)
                {
                    return RelayLiveLoopResult<HandshakeChallenge>.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.Busy, "handshake", "Challenge capacity is full.", true));
                }

                var challenge = new HandshakeChallenge(
                    RandomToken(24),
                    clientNonce,
                    RandomToken(32),
                    revision.Value,
                    now.Add(_challengeLifetime));
                _challenges.Add(challenge.ChallengeId, challenge);
                return RelayLiveLoopResult<HandshakeChallenge>.Success(challenge);
            }
        }

        public RelayLiveLoopResult<AuthenticatedConnection> CompleteHandshake(string challengeId, string proof)
        {
            HandshakeChallenge challenge;
            lock (_sync)
            {
                ThrowIfDisposed();
                var now = _utcNow();
                PruneExpired(now);
                if (string.IsNullOrEmpty(challengeId) || !_challenges.TryGetValue(challengeId, out challenge))
                {
                    return RelayLiveLoopResult<AuthenticatedConnection>.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.AuthRequired, "handshake", "Challenge is missing or expired.", true));
                }

                _challenges.Remove(challengeId);
                var current = _identity.RuntimeRevisions.CaptureIfCurrent(
                    challenge.RuntimeRevision,
                    "handshake");
                if (!current.Succeeded)
                {
                    return RelayLiveLoopResult<AuthenticatedConnection>.Failure(current.Error);
                }

                var expected = ComputeHandshakeProof(_sharedSecret, _identity, challenge);
                if (!FixedTimeBase64Equals(expected, proof))
                {
                    return RelayLiveLoopResult<AuthenticatedConnection>.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.AuthRequired, "handshake", "Handshake proof is invalid.", false));
                }

                if (_connections.Count >= _maximumConnections)
                {
                    return RelayLiveLoopResult<AuthenticatedConnection>.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.Busy, "handshake", "Connection capacity is full.", true));
                }

                var connectionId = RandomToken(24);
                var expiry = now.Add(_connectionLifetime);
                _connections.Add(connectionId, new ConnectionState
                {
                    Key = DeriveConnectionKey(_sharedSecret, _identity, challenge),
                    ExpiresAtUtc = expiry,
                    LastSequence = 0
                });
                return RelayLiveLoopResult<AuthenticatedConnection>.Success(
                    new AuthenticatedConnection(connectionId, expiry));
            }
        }

        public RelayLiveLoopResult AuthenticateRequest(RequestAuthentication authentication)
        {
            if (authentication == null)
            {
                return RelayLiveLoopResult.Failure(
                    RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.AuthRequired, "authenticate", "Request authentication is required.", false));
            }

            try
            {
                RuntimeSessionIdentity.RequireCanonicalValue(authentication.ConnectionId, "connectionId");
                RuntimeSessionIdentity.RequireCanonicalValue(authentication.RequestId, "requestId");
                RuntimeSessionIdentity.RequireCanonicalValue(authentication.Operation, "operation");
                RuntimeSessionIdentity.RequireCanonicalValue(authentication.PayloadSha256, "payloadSha256");
            }
            catch (ArgumentException ex)
            {
                return RelayLiveLoopResult.Failure(
                    RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.InvalidMessage, "authenticate", ex.Message, false));
            }

            lock (_sync)
            {
                ThrowIfDisposed();
                var now = _utcNow();
                PruneExpired(now);
                ConnectionState state;
                if (!_connections.TryGetValue(authentication.ConnectionId, out state))
                {
                    return RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.AuthRequired, "authenticate", "Connection is missing or expired.", true));
                }

                if (authentication.Sequence <= state.LastSequence)
                {
                    return RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.AuthRequired, "authenticate", "Sequence was replayed or reordered.", false));
                }

                DateTimeOffset sentAt;
                try
                {
                    sentAt = DateTimeOffset.FromUnixTimeMilliseconds(authentication.SentAtUnixMilliseconds);
                }
                catch (ArgumentOutOfRangeException)
                {
                    return RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.InvalidMessage, "authenticate", "Request timestamp is invalid.", false));
                }

                if ((now - sentAt).Duration() > _maximumClockSkew)
                {
                    return RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.AuthRequired, "authenticate", "Request timestamp is outside the allowed skew.", true));
                }

                var expected = ComputeRequestProof(state.Key, authentication);
                if (!FixedTimeBase64Equals(expected, authentication.Proof))
                {
                    return RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.AuthRequired, "authenticate", "Request proof is invalid.", false));
                }

                state.LastSequence = authentication.Sequence;
                return RelayLiveLoopResult.Success();
            }
        }

        public void CloseConnection(string connectionId)
        {
            if (string.IsNullOrEmpty(connectionId)) return;
            lock (_sync)
            {
                ConnectionState state;
                if (_connections.TryGetValue(connectionId, out state))
                {
                    ClearBytes(state.Key);
                    _connections.Remove(connectionId);
                }
            }
        }

        public void Dispose()
        {
            lock (_sync)
            {
                if (_disposed) return;
                foreach (var state in _connections.Values) ClearBytes(state.Key);
                _connections.Clear();
                _challenges.Clear();
                ClearBytes(_sharedSecret);
                _disposed = true;
            }
        }

        public static string ComputeHandshakeProof(
            byte[] sharedSecret,
            RuntimeSessionIdentity identity,
            HandshakeChallenge challenge)
        {
            if (sharedSecret == null) throw new ArgumentNullException(nameof(sharedSecret));
            if (identity == null) throw new ArgumentNullException(nameof(identity));
            if (challenge == null) throw new ArgumentNullException(nameof(challenge));
            return ComputeHmacBase64(sharedSecret, BuildHandshakeCanonical(identity, challenge));
        }

        public static byte[] DeriveConnectionKey(
            byte[] sharedSecret,
            RuntimeSessionIdentity identity,
            HandshakeChallenge challenge)
        {
            if (sharedSecret == null) throw new ArgumentNullException(nameof(sharedSecret));
            using (var hmac = new HMACSHA256(sharedSecret))
            {
                return hmac.ComputeHash(Encoding.UTF8.GetBytes("connection\n" + BuildHandshakeCanonical(identity, challenge)));
            }
        }

        public static string ComputeRequestProof(byte[] connectionKey, RequestAuthentication authentication)
        {
            if (connectionKey == null) throw new ArgumentNullException(nameof(connectionKey));
            if (authentication == null) throw new ArgumentNullException(nameof(authentication));
            return ComputeHmacBase64(connectionKey, BuildRequestCanonical(authentication));
        }

        public static string ComputeSha256(byte[] payload)
        {
            if (payload == null) throw new ArgumentNullException(nameof(payload));
            using (var sha = SHA256.Create())
            {
                var bytes = sha.ComputeHash(payload);
                var builder = new StringBuilder(bytes.Length * 2);
                for (var index = 0; index < bytes.Length; index++)
                {
                    builder.Append(bytes[index].ToString("x2", CultureInfo.InvariantCulture));
                }

                return builder.ToString();
            }
        }

        private static string BuildHandshakeCanonical(RuntimeSessionIdentity identity, HandshakeChallenge challenge)
        {
            return string.Join("\n", new[]
            {
                "RelayLiveLoop/1",
                identity.ProtocolVersion.ToString(CultureInfo.InvariantCulture),
                identity.SessionId,
                identity.LaunchId,
                challenge.RuntimeRevision,
                challenge.ChallengeId,
                challenge.ClientNonce,
                challenge.ServerNonce
            });
        }

        private static string BuildRequestCanonical(RequestAuthentication authentication)
        {
            return string.Join("\n", new[]
            {
                "RelayLiveLoop/request/1",
                authentication.ConnectionId,
                authentication.Sequence.ToString(CultureInfo.InvariantCulture),
                authentication.SentAtUnixMilliseconds.ToString(CultureInfo.InvariantCulture),
                authentication.RequestId,
                authentication.Operation,
                authentication.PayloadSha256
            });
        }

        private static string ComputeHmacBase64(byte[] key, string canonical)
        {
            using (var hmac = new HMACSHA256(key))
            {
                return Convert.ToBase64String(hmac.ComputeHash(Encoding.UTF8.GetBytes(canonical)));
            }
        }

        private static bool FixedTimeBase64Equals(string expected, string supplied)
        {
            byte[] left;
            byte[] right;
            try
            {
                left = Convert.FromBase64String(expected ?? string.Empty);
                right = Convert.FromBase64String(supplied ?? string.Empty);
            }
            catch (FormatException)
            {
                return false;
            }

            var difference = left.Length ^ right.Length;
            var length = Math.Max(left.Length, right.Length);
            for (var index = 0; index < length; index++)
            {
                var leftByte = index < left.Length ? left[index] : (byte)0;
                var rightByte = index < right.Length ? right[index] : (byte)0;
                difference |= leftByte ^ rightByte;
            }

            return difference == 0;
        }

        private static string RandomToken(int byteCount)
        {
            var bytes = new byte[byteCount];
            using (var random = RandomNumberGenerator.Create()) random.GetBytes(bytes);
            return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }

        private void PruneExpired(DateTimeOffset now)
        {
            var expiredChallenges = new List<string>();
            foreach (var pair in _challenges)
            {
                if (pair.Value.ExpiresAtUtc <= now) expiredChallenges.Add(pair.Key);
            }

            foreach (var id in expiredChallenges) _challenges.Remove(id);

            var expiredConnections = new List<string>();
            foreach (var pair in _connections)
            {
                if (pair.Value.ExpiresAtUtc <= now) expiredConnections.Add(pair.Key);
            }

            foreach (var id in expiredConnections) CloseConnection(id);
        }

        private static void ClearBytes(byte[] bytes)
        {
            if (bytes == null) return;
            for (var index = 0; index < bytes.Length; index++) bytes[index] = 0;
        }

        private void ThrowIfDisposed()
        {
            if (_disposed) throw new ObjectDisposedException(nameof(SessionAuthentication));
        }
    }
}
#endif
