#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RelayLiveLoop
{
    public interface IRelayLiveLoopMainThreadGuard
    {
        bool IsMainThread { get; }
        void AssertMainThread();
    }

    public sealed class RelayLiveLoopMainThreadDispatcher : MonoBehaviour, IRelayLiveLoopMainThreadGuard
    {
        private sealed class WorkItem
        {
            public string RequestId;
            public Func<RelayLiveLoopResult> Work;
            public DateTimeOffset DeadlineUtc;
            public CancellationToken CancellationToken;
            public TaskCompletionSource<RelayLiveLoopResult> Completion;
        }

        private readonly ConcurrentQueue<WorkItem> _queue = new ConcurrentQueue<WorkItem>();
        private int _mainThreadId;
        private int _pendingCount;
        private int _maximumPending = 64;
        private int _maximumItemsPerFrame = 8;
        private double _maximumMillisecondsPerFrame = 3.0;
        private bool _initialized;
        private bool _stopping;

        public bool IsMainThread
        {
            get { return _initialized && Thread.CurrentThread.ManagedThreadId == _mainThreadId; }
        }

        public int PendingCount { get { return Volatile.Read(ref _pendingCount); } }

        public void InitializeOnMainThread(
            int maximumPending = 64,
            int maximumItemsPerFrame = 8,
            double maximumMillisecondsPerFrame = 3.0)
        {
            if (maximumPending <= 0) throw new ArgumentOutOfRangeException(nameof(maximumPending));
            if (maximumItemsPerFrame <= 0) throw new ArgumentOutOfRangeException(nameof(maximumItemsPerFrame));
            if (maximumMillisecondsPerFrame <= 0 || maximumMillisecondsPerFrame > 50)
            {
                throw new ArgumentOutOfRangeException(nameof(maximumMillisecondsPerFrame));
            }

            if (_initialized && !IsMainThread)
            {
                throw new InvalidOperationException("Dispatcher can only be reconfigured on its owning main thread.");
            }

            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
            _maximumPending = maximumPending;
            _maximumItemsPerFrame = maximumItemsPerFrame;
            _maximumMillisecondsPerFrame = maximumMillisecondsPerFrame;
            _initialized = true;
            _stopping = false;
        }

        public Task<RelayLiveLoopResult> Schedule(
            string requestId,
            Func<RelayLiveLoopResult> work,
            TimeSpan timeout,
            CancellationToken cancellationToken)
        {
            if (!_initialized) throw new InvalidOperationException("Dispatcher is not initialized.");
            if (string.IsNullOrWhiteSpace(requestId)) throw new ArgumentException("Request id is required.", nameof(requestId));
            if (work == null) throw new ArgumentNullException(nameof(work));
            if (timeout <= TimeSpan.Zero || timeout > TimeSpan.FromMinutes(5))
            {
                throw new ArgumentOutOfRangeException(nameof(timeout));
            }

            if (_stopping)
            {
                return Task.FromResult(RelayLiveLoopResult.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StateUnknown,
                        "main_thread_queue",
                        "Dispatcher is stopping.",
                        true,
                        null)));
            }

            var pending = Interlocked.Increment(ref _pendingCount);
            if (pending > _maximumPending)
            {
                Interlocked.Decrement(ref _pendingCount);
                return Task.FromResult(RelayLiveLoopResult.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.Busy,
                        "main_thread_queue",
                        "Main-thread queue capacity is full.",
                        true)));
            }

            var completion = new TaskCompletionSource<RelayLiveLoopResult>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            _queue.Enqueue(new WorkItem
            {
                RequestId = requestId,
                Work = work,
                DeadlineUtc = DateTimeOffset.UtcNow.Add(timeout),
                CancellationToken = cancellationToken,
                Completion = completion
            });
            return completion.Task;
        }

        public void AssertMainThread()
        {
            if (!IsMainThread)
            {
                throw new InvalidOperationException("Unity object access must execute through the Relay LiveLoop main-thread dispatcher.");
            }
        }

        internal void DrainOnce()
        {
            AssertMainThread();
            var stopwatch = Stopwatch.StartNew();
            var processed = 0;
            WorkItem item;
            while (processed < _maximumItemsPerFrame &&
                   stopwatch.Elapsed.TotalMilliseconds < _maximumMillisecondsPerFrame &&
                   _queue.TryDequeue(out item))
            {
                Interlocked.Decrement(ref _pendingCount);
                processed++;
                if (item.CancellationToken.IsCancellationRequested)
                {
                    item.Completion.TrySetCanceled();
                    continue;
                }

                if (DateTimeOffset.UtcNow > item.DeadlineUtc)
                {
                    item.Completion.TrySetResult(RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.Timeout,
                            "main_thread_queue",
                            "Request expired before main-thread execution.",
                            true)));
                    continue;
                }

                try
                {
                    var result = item.Work();
                    item.Completion.TrySetResult(result ?? RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.InternalError,
                            "main_thread_execute",
                            "Main-thread handler returned no result.",
                            false,
                            null)));
                }
                catch (Exception ex)
                {
                    item.Completion.TrySetResult(RelayLiveLoopResult.Failure(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.InternalError,
                            "main_thread_execute",
                            ex.GetType().Name + ": " + ex.Message,
                            false,
                            null)));
                }
            }
        }

        private void Awake()
        {
            if (!_initialized) InitializeOnMainThread();
        }

        private void Update()
        {
            if (_initialized && !_stopping) DrainOnce();
        }

        private void OnDestroy()
        {
            _stopping = true;
            WorkItem item;
            while (_queue.TryDequeue(out item))
            {
                Interlocked.Decrement(ref _pendingCount);
                item.Completion.TrySetResult(RelayLiveLoopResult.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StateUnknown,
                        "main_thread_queue",
                        "Dispatcher was destroyed before execution.",
                        true,
                        null)));
            }
        }
    }
}
#endif
