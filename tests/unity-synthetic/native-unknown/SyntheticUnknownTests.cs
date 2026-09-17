#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.IO;
using System.Text;
using System.Threading;
using RelayLiveLoop;

internal static class SyntheticUnknownTests
{
    private const string RequestId = "runtime_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    public static int Main(string[] args)
    {
        try
        {
            if (args == null || args.Length != 1 || string.IsNullOrWhiteSpace(args[0]))
            {
                throw new ArgumentException("One output path is required.");
            }

            VerifyFourArgumentSessionIdentityCompatibility();
            var wire = CreateActualAdapterErrorWire();
            File.WriteAllBytes(args[0], wire);
            Console.WriteLine("ACTUAL C# ADAPTER PASS: nullable neutral attribution serialized as an unknown error envelope.");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("ACTUAL C# ADAPTER FAIL: " + exception);
            return 1;
        }
    }

    private static void VerifyFourArgumentSessionIdentityCompatibility()
    {
        var legacy = new RuntimeSessionIdentity("session-legacy", "launch-legacy", "revision-legacy", 1);
        var resourceBound = new RuntimeSessionIdentity("session-bound", "launch-bound", "revision-bound", 1, "release-neutral");
        Equal(null, legacy.ResourceReleaseId, "legacy constructor leaves resource release absent");
        Equal("release-neutral", resourceBound.ResourceReleaseId, "extended constructor binds resource release");
    }

    private static byte[] CreateActualAdapterErrorWire()
    {
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread();
        var secret = new byte[32];
        for (var index = 0; index < secret.Length; index++) secret[index] = (byte)(index + 1);
        var identity = new RuntimeSessionIdentity("session-native-unknown", "launch-native-unknown", "revision-native-unknown", 1, "release-native-unknown");
        using (var bridge = new RuntimeBridgeCore(identity, secret, dispatcher))
        {
            var challengeResult = bridge.Authentication.BeginHandshake(
                identity.SessionId,
                identity.LaunchId,
                identity.RuntimeRevision,
                identity.ProtocolVersion,
                "client-native-unknown");
            True(challengeResult.Succeeded, "actual bridge issues a handshake challenge");
            var challenge = challengeResult.Value;
            var key = SessionAuthentication.DeriveConnectionKey(secret, identity, challenge);
            var handshakeProof = SessionAuthentication.ComputeHandshakeProof(secret, identity, challenge);
            var connectionResult = bridge.Authentication.CompleteHandshake(challenge.ChallengeId, handshakeProof);
            True(connectionResult.Succeeded, "actual bridge authenticates the synthetic connection");

            var payload = Encoding.UTF8.GetBytes("{}");
            var unsigned = new RequestAuthentication(
                connectionResult.Value.ConnectionId,
                1,
                DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                RequestId,
                "module.quiesce",
                SessionAuthentication.ComputeSha256(payload),
                string.Empty);
            var authentication = new RequestAuthentication(
                unsigned.ConnectionId,
                unsigned.Sequence,
                unsigned.SentAtUnixMilliseconds,
                unsigned.RequestId,
                unsigned.Operation,
                unsigned.PayloadSha256,
                SessionAuthentication.ComputeRequestProof(key, unsigned));
            var request = new AuthenticatedRuntimeRequest(
                identity.SessionId,
                identity.RuntimeRevision,
                authentication,
                payload);
            var adapter = new RuntimeTransportCommandAdapter(bridge, new UnknownTerminalHandler());
            var scheduled = adapter.Schedule(request, CancellationToken.None);
            dispatcher.DrainOnce();
            var terminal = scheduled.GetAwaiter().GetResult() as RuntimeTransportExecutionResult;
            True(terminal != null, "actual command adapter returns its transport result");
            True(!terminal.Succeeded, "unknown neutral result is not returned as success");
            Equal(RelayLiveLoopErrorCode.StateUnknown, terminal.Error.Code, "unknown neutral result becomes STATE_UNKNOWN");
            Equal(null, terminal.Error.RuntimeChanged, "unknown neutral result retains nullable attribution");

            var encoded = RuntimeTransportWireCodec.Serialize(
                RuntimeTransportWireMessage.ErrorResponse(RequestId, terminal.Error));
            var decoded = RuntimeTransportWireCodec.Deserialize(encoded);
            Equal("error", decoded.Kind, "wire codec emits an error terminal");
            Equal(RequestId, decoded.RequestId, "wire terminal retains exact request identity");
            Equal(false, decoded.RuntimeChangedKnown, "outer wire does not claim known attribution");
            Equal(false, decoded.Error.RuntimeChangedKnown, "error wire does not claim known attribution");
            Equal("STATE_UNKNOWN", decoded.Error.Code, "wire error retains STATE_UNKNOWN");
            return encoded;
        }
    }

    private sealed class UnknownTerminalHandler : IRuntimeTransportCommandHandler
    {
        public RelayLiveLoopResult<NeutralPayload> Execute(RuntimeTransportCommandContext request)
        {
            const string json = "{\"status\":\"failed\",\"runtimeChanged\":null,\"error\":{\"code\":\"STATE_UNKNOWN\"}}";
            return RelayLiveLoopResult<NeutralPayload>.Success(new NeutralPayload(
                "relay.liveloop.command-result",
                1,
                "application/json",
                Encoding.UTF8.GetBytes(json)));
        }
    }

    private static void True(bool value, string message)
    {
        if (!value) throw new InvalidOperationException(message);
    }

    private static void Equal<T>(T expected, T actual, string message)
    {
        if (!object.Equals(expected, actual))
        {
            var expectedObject = (object)expected;
            var actualObject = (object)actual;
            throw new InvalidOperationException(message + ": expected " + (expectedObject == null ? "<null>" : expectedObject.ToString()) +
                ", got " + (actualObject == null ? "<null>" : actualObject.ToString()));
        }
    }
}
#endif
