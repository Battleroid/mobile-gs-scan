package dev.battleroid.mobilegsscan.quality

import com.google.ar.core.Pose
import com.google.ar.core.TrackingState
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.acos
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * On-device frame-quality gate that decides whether each
 * ARCore-tracked frame should land in the upload set or be dropped.
 *
 * The current capture pipeline writes *every* frame ARCore plus
 * the throttle gate hands us — there is no quality check between
 * ``ARCaptureSession.pollFrameData`` and ``DraftStore.appendFrame``.
 * That floods the dataset with motion-blurred frames (during pans),
 * exposure-jittered frames (when auto-exposure resettles after a
 * scene change), and crushed / blown frames (mixed-lit scenes
 * pointing camera at a window). All of those degrade the trained
 * splat — splatfacto has no input-quality awareness and weights
 * every frame equally during optimization.
 *
 * This filter runs four signals and rejects any frame that fails
 * one. The native kernel (see [NativeKernels]) does the Y-plane
 * sweep in a single sub-millisecond pass; pose checks are O(1).
 *
 *   * **Sharpness** — variance of Laplacian on the downsampled Y
 *     plane. Drops frames with ``varL < blurThreshold`` AND below
 *     ``0.6 * runningEMA`` (the relative gate catches motion-blur
 *     transients in a scene where most frames are sharp).
 *   * **Motion** — angular + linear velocity from pose deltas
 *     against the last accepted frame. ARCore world is metric —
 *     ``Session.createAnchor`` distances are real meters — so the
 *     thresholds are direct: 0.05–0.65 m/s linear, 5–45 deg/s
 *     angular.
 *   * **Exposure** — Y mean shift against an EMA of recent
 *     accepted frames. A jump larger than ``exposureSigma`` times
 *     the running standard deviation flags auto-exposure mid-
 *     adjustment. Plus crushed (>40% dark-clip) and blown (>25%
 *     bright-clip) histograms.
 *   * **Tracking + pre-roll** — already gated to TRACKING upstream,
 *     but we *also* drop the first ``PRE_ROLL_FRAMES`` frames
 *     after each TRACKING resume. ARCore's pose is noisy for the
 *     first ~250 ms post-resume even though it reports TRACKING.
 *
 * State (per session): running EMAs for sharpness + luminance,
 * last-accepted pose + ts, post-tracking pre-roll counter, and
 * atomic drop counters the HUD reads. The filter is single-
 * threaded by construction — it lives on the GL render thread —
 * but the counters are atomic so the UI thread can read them
 * without a fence.
 *
 * Cold-start (first [WARMUP_FRAMES] accepted frames):
 *   * relative blur gate disabled (no EMA yet)
 *   * exposure-jitter gate disabled (same reason)
 *   * absolute blur floor + clipping gates + motion gate +
 *     pre-roll gate are all live.
 *
 * Configurable via the [Config] data class, snapshotted at session
 * start. Live-edit not required — the user typically tunes once
 * in Settings, not mid-capture.
 */
class FrameQualityFilter(private val cfg: Config) {

    data class Config(
        val enabled: Boolean = true,
        /** Absolute Laplacian-variance floor. ~80 catches obviously
         *  soft frames without rejecting legitimate low-contrast
         *  scenes (uniform walls, neutral skies). */
        val blurThreshold: Double = 80.0,
        /** Max linear pose velocity (m/s). */
        val linVelMax: Double = 0.35,
        /** Max angular pose velocity (deg/s). */
        val angVelMaxDeg: Double = 25.0,
        /** Multiplier on running Y stddev — drops frames whose mean
         *  jumped more than this many sigmas from the EMA. */
        val exposureSigma: Double = 3.0,
        /** Maximum fraction of Y samples allowed in the bottom histogram bin. */
        val darkClipMax: Double = 0.40,
        /** Maximum fraction of Y samples allowed in the top histogram bin. */
        val brightClipMax: Double = 0.25,
        /** Minimum ms between accepted frames (additional rate-limit
         *  on top of ARCore + the FPS slider). 0 disables. */
        val minIntervalMs: Long = 0L,
    )

    sealed class Decision {
        object Accept : Decision()
        data class Drop(val reason: DropReason) : Decision()
    }

    enum class DropReason { BLUR, MOTION, EXPOSURE, TRACKING, PRE_ROLL, RATE_LIMIT, KERNEL_UNAVAILABLE }

    private val blurCount = AtomicInteger(0)
    private val motionCount = AtomicInteger(0)
    private val exposureCount = AtomicInteger(0)
    private val trackingCount = AtomicInteger(0)
    private val preRollCount = AtomicInteger(0)
    private val rateLimitCount = AtomicInteger(0)
    // Latched 0 → 1 the first time the native kernel is missing.
    // This counter does NOT participate in [DropCounts.total]
    // because the kernel-missing path *accepts* the frame; it's
    // purely a "filter degraded" flag the HUD / telemetry can
    // surface separately.
    private val kernelMissingCount = AtomicInteger(0)
    private val acceptCount = AtomicInteger(0)

    // Per-session running stats; updated on accept only so they
    // don't drift toward the rejected frames' distribution.
    private var sharpnessEma: Double = 0.0
    private var lumaEma: Double = 0.0
    private var lumaVarEma: Double = 0.0
    private var warmupFrames: Int = 0

    private var lastAcceptedPose: Pose? = null
    private var lastAcceptedTsNs: Long = 0L
    private var lastTrackingState: TrackingState? = null
    private var framesSinceTrackingResume: Int = Int.MAX_VALUE

    fun snapshot(): DropCounts = DropCounts(
        blur = blurCount.get(),
        motion = motionCount.get(),
        exposure = exposureCount.get(),
        tracking = trackingCount.get(),
        preRoll = preRollCount.get(),
        rateLimit = rateLimitCount.get(),
        kernelMissing = kernelMissingCount.get(),
        accepted = acceptCount.get(),
    )

    /**
     * Notify the filter of the current ARCore tracking state on
     * *every* frame — including frames where [ARCaptureSession.
     * acquireRawFrame] returned null (no image yet, throttle gate,
     * non-TRACKING). Without this, the filter only sees frames
     * that already passed the upstream TRACKING check, so it
     * never observes a TRACKING → PAUSED transition; the pre-roll
     * reset path that's supposed to fire on resume then never
     * arms, and the user gets noisy post-resume frames in the
     * dataset.
     *
     * Idempotent — calling with the current state is a no-op. Cost
     * is one compare + one assign on every call; safe to drop
     * into the GL render thread's hot path.
     */
    fun notifyTrackingState(trackingState: TrackingState) {
        if (lastTrackingState == trackingState) return
        if (trackingState != TrackingState.TRACKING) {
            trackingCount.incrementAndGet()
            framesSinceTrackingResume = 0
        }
        lastTrackingState = trackingState
    }

    /**
     * Decide whether a single frame should be accepted.
     *
     * Caller is expected to keep the ``android.media.Image`` (the
     * source of [yBuffer]) alive for the duration of this call —
     * the buffer points into the image's native memory and reads
     * past close are undefined.
     *
     * [trackingState] should be the current ARCore frame's
     * tracking state; the filter handles the transition tracking
     * itself.
     */
    fun evaluate(
        yBuffer: ByteBuffer,
        width: Int,
        height: Int,
        rowStride: Int,
        pixelStride: Int,
        pose: Pose,
        timestampNs: Long,
        trackingState: TrackingState,
    ): Decision {
        if (!cfg.enabled) {
            return acceptInternal(timestampNs, pose, accumulateStats = false)
        }

        // Tracking gate (defensive — the upstream throttle in
        // ``ARCaptureSession.pollFrameData`` already drops
        // non-tracking frames, but this filter is meant to be
        // usable from any call site so re-check here).
        if (trackingState != TrackingState.TRACKING) {
            trackingCount.incrementAndGet()
            framesSinceTrackingResume = 0
            lastTrackingState = trackingState
            return Decision.Drop(DropReason.TRACKING)
        }
        if (lastTrackingState != TrackingState.TRACKING) {
            framesSinceTrackingResume = 0
        }
        lastTrackingState = trackingState

        // Pre-roll: ARCore's pose drifts visibly for the first
        // ~250 ms after a TRACKING resume even though it self-
        // reports as tracking. Skip those frames so motion-velocity
        // calculations against them don't poison the dataset.
        if (framesSinceTrackingResume < PRE_ROLL_FRAMES) {
            ++framesSinceTrackingResume
            preRollCount.incrementAndGet()
            return Decision.Drop(DropReason.PRE_ROLL)
        }

        // Rate limit: optional gap enforcer (0 disables). When set
        // it caps the *accepted* frame rate independently of the
        // underlying capture rate — handy if you want to run the
        // camera at 30 fps for stability but only keep every other
        // sharp frame.
        if (cfg.minIntervalMs > 0 && lastAcceptedTsNs > 0L) {
            val gapMs = (timestampNs - lastAcceptedTsNs) / 1_000_000L
            if (gapMs < cfg.minIntervalMs) {
                rateLimitCount.incrementAndGet()
                return Decision.Drop(DropReason.RATE_LIMIT)
            }
        }

        // Native kernel pass. Returns null when the .so failed to
        // load — instead of crashing the pipeline, we accept the
        // frame anyway so capture is at least usable. The counter
        // is latched at 1 so the HUD / telemetry can surface
        // "filter degraded" once, rather than flooding the total
        // with one bogus drop per frame (which used to make the
        // HUD report large "dropped" totals on devices that
        // weren't actually dropping anything).
        val stats = NativeKernels.analyze(yBuffer, width, height, rowStride, pixelStride)
            ?.let(NativeKernels.Stats::fromArray)
        if (stats == null) {
            kernelMissingCount.compareAndSet(0, 1)
            return acceptInternal(timestampNs, pose, accumulateStats = false)
        }

        // Exposure clipping is a hard fail regardless of warmup —
        // a frame with 40%+ pixels at full black or 25%+ at full
        // white never produces useful splat training signal.
        if (stats.darkClipPct > cfg.darkClipMax || stats.brightClipPct > cfg.brightClipMax) {
            exposureCount.incrementAndGet()
            return Decision.Drop(DropReason.EXPOSURE)
        }

        // Sharpness — absolute floor (always on) + relative gate
        // against the EMA (only after warmup, otherwise a sharp
        // first frame would gate every subsequent frame at 60% of
        // its score and the filter would never settle).
        if (stats.laplacianVariance < cfg.blurThreshold) {
            blurCount.incrementAndGet()
            return Decision.Drop(DropReason.BLUR)
        }
        if (warmupFrames >= WARMUP_FRAMES && stats.laplacianVariance < RELATIVE_BLUR_RATIO * sharpnessEma) {
            blurCount.incrementAndGet()
            return Decision.Drop(DropReason.BLUR)
        }

        // Exposure jitter — only after warmup so the first few
        // accepts seed the EMA without rejecting themselves.
        if (warmupFrames >= WARMUP_FRAMES) {
            val sigma = sqrt(max(lumaVarEma, 1.0))
            if (Math.abs(stats.yMean - lumaEma) > cfg.exposureSigma * sigma) {
                exposureCount.incrementAndGet()
                return Decision.Drop(DropReason.EXPOSURE)
            }
        }

        // Motion — only against an existing last-accepted pose.
        // The first accepted frame seeds it (no baseline yet).
        lastAcceptedPose?.let { prev ->
            val dt = (timestampNs - lastAcceptedTsNs) / 1e9
            if (dt > 0) {
                val (linVel, angVelDeg) = velocity(prev, pose, dt)
                if (linVel > cfg.linVelMax || angVelDeg > cfg.angVelMaxDeg) {
                    motionCount.incrementAndGet()
                    return Decision.Drop(DropReason.MOTION)
                }
            }
        }

        // Update running stats *only on accept* so the EMAs track
        // the distribution of kept frames, not the input stream.
        sharpnessEma = ema(sharpnessEma, stats.laplacianVariance, EMA_ALPHA, warmupFrames)
        lumaEma = ema(lumaEma, stats.yMean, EMA_ALPHA, warmupFrames)
        lumaVarEma = ema(lumaVarEma, stats.yVariance, EMA_ALPHA, warmupFrames)

        return acceptInternal(timestampNs, pose, accumulateStats = true)
    }

    private fun acceptInternal(
        timestampNs: Long,
        pose: Pose,
        accumulateStats: Boolean,
    ): Decision {
        lastAcceptedPose = pose
        lastAcceptedTsNs = timestampNs
        if (accumulateStats && warmupFrames < WARMUP_FRAMES) {
            ++warmupFrames
        }
        acceptCount.incrementAndGet()
        return Decision.Accept
    }

    private fun ema(current: Double, sample: Double, alpha: Double, warmup: Int): Double =
        if (warmup == 0) sample
        else (1.0 - alpha) * current + alpha * sample

    private fun velocity(prev: Pose, curr: Pose, dt: Double): Pair<Double, Double> {
        val dx = curr.tx() - prev.tx()
        val dy = curr.ty() - prev.ty()
        val dz = curr.tz() - prev.tz()
        val linVel = sqrt((dx * dx + dy * dy + dz * dz).toDouble()) / dt

        // Quaternion difference → rotation angle in degrees. Using
        // dot product magnitude; the absolute value handles the
        // double-cover of unit quaternions (q and -q represent the
        // same rotation).
        val dot = (prev.qx() * curr.qx()
            + prev.qy() * curr.qy()
            + prev.qz() * curr.qz()
            + prev.qw() * curr.qw())
        val clamped = min(1.0, max(-1.0, Math.abs(dot.toDouble())))
        val angRad = 2.0 * acos(clamped)
        val angVelDeg = Math.toDegrees(angRad) / dt
        return linVel to angVelDeg
    }

    companion object {
        private const val EMA_ALPHA: Double = 0.15
        private const val RELATIVE_BLUR_RATIO: Double = 0.6
        private const val WARMUP_FRAMES: Int = 30
        private const val PRE_ROLL_FRAMES: Int = 8
    }
}

data class DropCounts(
    val blur: Int = 0,
    val motion: Int = 0,
    val exposure: Int = 0,
    val tracking: Int = 0,
    val preRoll: Int = 0,
    val rateLimit: Int = 0,
    /** Latched 0 → 1 flag — *not* a drop count. The kernel-
     *  missing path accepts the frame, so this would only inflate
     *  the HUD's drop total without representing real drops. The
     *  field is here so telemetry / debug surfaces can still
     *  report "filter degraded" once. */
    val kernelMissing: Int = 0,
    val accepted: Int = 0,
) {
    val total: Int get() = blur + motion + exposure + tracking + preRoll + rateLimit

    /** Dominant reason among "user-meaningful" drops (excludes
     *  pre-roll and tracking, which the user can't react to). */
    fun dominantReason(): FrameQualityFilter.DropReason? = listOf(
        FrameQualityFilter.DropReason.MOTION to motion,
        FrameQualityFilter.DropReason.BLUR to blur,
        FrameQualityFilter.DropReason.EXPOSURE to exposure,
    ).maxByOrNull { it.second }?.takeIf { it.second > 0 }?.first
}
