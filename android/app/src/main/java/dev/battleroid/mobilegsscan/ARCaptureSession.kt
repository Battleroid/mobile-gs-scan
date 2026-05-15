package dev.battleroid.mobilegsscan

import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.media.Image
import com.google.ar.core.Camera
import com.google.ar.core.CameraIntrinsics
import com.google.ar.core.Config
import com.google.ar.core.Frame
import com.google.ar.core.LightEstimate
import com.google.ar.core.PointCloud
import com.google.ar.core.Pose
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.NotYetAvailableException
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer

/**
 * Thin wrapper over an ARCore [Session] that:
 *   - configures the session for fastest CPU image access (the GPU
 *     image pipeline is too involved for the current overlay; we
 *     keep the overlay minimal and read CPU-side YUV instead).
 *   - exposes [update] which advances the session by one frame and
 *     returns the resulting [Frame], so callers can run their own
 *     rendering against ARCore's camera-feed texture.
 *   - exposes [acquireRawFrame] which extracts the most recent
 *     tracked pose + intrinsics + ARCore [Image] from a Frame,
 *     throttled to a target frame rate. Two-stage: callers can
 *     inspect the YUV / pose, decide whether to keep the frame,
 *     and only pay for the YUV→JPEG encode on accept.
 *   - exposes [viewMatrix] / [projectionMatrix] / [acquirePointCloud]
 *     so overlay renderers can project and color world-space
 *     geometry without reaching into the underlying [Session].
 *
 * Designed to be driven from the `GLSurfaceView` render thread.
 */
class ARCaptureSession(
    context: Context,
    /** App-side fps throttle when ARCore's CameraConfig isn't pinning
     *  the frame rate. Used as ``targetIntervalMs`` only when the
     *  preset key didn't resolve to a supported CameraConfig (Custom
     *  or stale key); when a preset DOES apply, the constructor
     *  switches to no throttle so ARCore's hardware pacing is the
     *  sole rate-limit. */
    private val customIntervalMs: Long = 200, // 5 fps default
    private val jpegQuality: Int = 85,
    /** ARCore CameraConfig preset id; matches the format in
     *  [ServerConfig.cameraConfigKey] (``<w>x<h>@<fps>`` or
     *  [ServerConfig.CAMERA_CONFIG_CUSTOM]). Resolved against the
     *  current Session's supported configs at construction time —
     *  a stale / unrecognised key (device changed, OS upgrade)
     *  falls through to the ARCore default. */
    cameraConfigKey: String = ServerConfig.CAMERA_CONFIG_CUSTOM,
) {

    /** Whether a fixed CameraConfig was actually applied. ``false``
     *  when the preset key was Custom OR stale (didn't match any
     *  device-supported config); used to choose the effective
     *  throttle interval below. Visible for tests / diagnostics. */
    val presetApplied: Boolean

    private val targetIntervalMs: Long

    private val session: Session = Session(context).apply {
        // Apply the user's preset BEFORE configure(cfg). ARCore
        // requires camera-config changes to land before the first
        // configure call on a given session; setting it afterwards
        // is silently ignored. resolveCameraConfig returns null on
        // unrecognised / Custom keys → no override, ARCore picks
        // its default.
        val resolved = resolveCameraConfig(this, cameraConfigKey)
        if (resolved != null) {
            setCameraConfig(resolved)
        }
        // Throttle decision: skip the app-side fps cap only when
        // ARCore is going to pace the camera itself at the resolved
        // preset's rate. A stale or Custom key falls back to the
        // user's slider value so we don't accidentally flood the
        // wire at ARCore's default rate.
        this@ARCaptureSession.presetApplied = (resolved != null)
        this@ARCaptureSession.targetIntervalMs =
            if (resolved != null) 0L else customIntervalMs
        val cfg = Config(this).apply {
            focusMode = Config.FocusMode.AUTO
            updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
            // AMBIENT_INTENSITY (rather than DISABLED) so callers
            // can cross-check the Y-plane exposure metric against
            // ARCore's post-auto-exposure brightness estimate.
            // ARCore would otherwise just skip the small AE
            // analysis the cost of which is negligible — the
            // expensive ``ENVIRONMENTAL_HDR`` mode is the one we
            // avoid here.
            lightEstimationMode = Config.LightEstimationMode.AMBIENT_INTENSITY
            planeFindingMode = Config.PlaneFindingMode.HORIZONTAL_AND_VERTICAL
        }
        configure(cfg)
    }
    private var lastEmitMs: Long = 0

    fun resume() = session.resume()
    fun pause() = session.pause()
    fun close() = session.close()

    fun setTextureName(name: Int) = session.setCameraTextureName(name)
    fun setDisplayGeometry(rotation: Int, width: Int, height: Int) =
        session.setDisplayGeometry(rotation, width, height)

    /**
     * Advance the ARCore session by one frame. Returns null on the
     * MissingGlContextException / texture-not-bound class of errors
     * so the caller can keep polling without crashing.
     */
    fun update(): Frame? = try {
        session.update()
    } catch (e: Exception) {
        null
    }

    /**
     * Column-major 4x4 view matrix (world → camera) for [frame].
     * Used by overlay renderers (e.g. CoverageRenderer) to project
     * world-space geometry into the camera view.
     */
    fun viewMatrix(frame: Frame): FloatArray =
        FloatArray(16).also { frame.camera.getViewMatrix(it, 0) }

    /**
     * Column-major 4x4 perspective-projection matrix for [frame],
     * matching the same near/far planes the camera quad uses.
     */
    fun projectionMatrix(
        frame: Frame,
        near: Float = 0.05f,
        far: Float = 100f,
    ): FloatArray =
        FloatArray(16).also { frame.camera.getProjectionMatrix(it, 0, near, far) }

    /**
     * Acquire the tracked feature point cloud from [frame]. The
     * returned [PointCloud] is `Closeable` — caller must close it
     * (idiomatic Kotlin: `acquirePointCloud(frame).use { ... }`).
     */
    fun acquirePointCloud(frame: Frame): PointCloud =
        frame.acquirePointCloud()

    /**
     * Acquire pose + intrinsics + the raw ARCore [Image] from
     * [frame], if and only if:
     *   - ARCore is currently TRACKING (so the pose is meaningful)
     *   - enough time has elapsed since the last emit to honour the
     *     [targetIntervalMs] rate limit. A non-positive interval
     *     disables the throttle entirely, letting the caller rely
     *     on ARCore's own pacing — used when a fixed CameraConfig
     *     preset pins the frame rate at the hardware level and an
     *     app-side cap would just drop perfectly good frames.
     *   - acquireCameraImage actually has a frame ready (NotYet on
     *     the first few calls is normal).
     *
     * Returns null in any of those cases. **Caller is responsible
     * for [RawFrame.close]** — the underlying ARCore image holds a
     * native ref that has to be released, which is why the call
     * site uses ``acquireRawFrame(frame)?.use { ... }``.
     *
     * This is the first stage of a two-stage capture: callers can
     * inspect the Y plane (sharpness / exposure stats) and the
     * pose deltas (motion velocity) before committing to the
     * expensive YUV→JPEG encode in [encodeJpeg]. Dropping a frame
     * is just letting the [RawFrame] close without calling
     * [encodeJpeg], saving ~10–15 ms per dropped frame on top of
     * the storage / upload savings.
     */
    fun acquireRawFrame(frame: Frame): RawFrame? {
        val camera: Camera = frame.camera
        if (camera.trackingState != TrackingState.TRACKING) return null

        val now = frame.timestamp / 1_000_000L
        if (targetIntervalMs > 0 && now - lastEmitMs < targetIntervalMs) return null

        val image: Image = try {
            frame.acquireCameraImage()
        } catch (e: NotYetAvailableException) {
            return null
        }
        lastEmitMs = now
        val intrinsics = readIntrinsics(camera.imageIntrinsics)
        return RawFrame(
            image = image,
            pose = camera.pose,
            intrinsics = intrinsics,
            timestampNs = frame.timestamp,
            trackingState = camera.trackingState,
            jpegQuality = jpegQuality,
        )
    }

    /**
     * Pixel-intensity estimate from ARCore's ambient-light probe
     * for [frame]. Range nominally 0..1 (auto-exposure pre-
     * correction); ``null`` when the estimate is unavailable
     * (first ~3 frames after [resume], or LightEstimate state ≠
     * VALID). Cheap to read — ARCore computes it every frame
     * anyway under [Config.LightEstimationMode.AMBIENT_INTENSITY].
     */
    fun ambientPixelIntensity(frame: Frame): Float? {
        val estimate: LightEstimate = frame.lightEstimate
        if (estimate.state != LightEstimate.State.VALID) return null
        return try { estimate.pixelIntensity } catch (_: Exception) { null }
    }

    private fun readIntrinsics(intr: CameraIntrinsics): Intrinsics {
        val focal = intr.focalLength
        val pp = intr.principalPoint
        val dim = intr.imageDimensions
        return Intrinsics(
            fx = focal[0],
            fy = focal[1],
            cx = pp[0],
            cy = pp[1],
            w = dim[0],
            h = dim[1],
        )
    }

    internal fun yuvToJpegInternal(image: Image, quality: Int): ByteArray =
        yuvToJpeg(image, quality)

    private fun yuvToJpeg(image: Image, quality: Int): ByteArray {
        // ARCore returns Y_8 + UV planes (NV21-friendly). We flatten
        // into a single byte[] in NV21 order then let YuvImage do the
        // JPEG encoding. Allocation cost is meaningful per-frame but
        // fine at 5 fps; the trainable bottleneck is the WS write.
        val width = image.width
        val height = image.height
        val planes = image.planes
        val ySize = planes[0].buffer.remaining()
        val uSize = planes[1].buffer.remaining()
        val vSize = planes[2].buffer.remaining()
        val nv21 = ByteArray(ySize + uSize + vSize)
        planes[0].buffer.get(nv21, 0, ySize)
        planes[2].buffer.get(nv21, ySize, vSize)
        planes[1].buffer.get(nv21, ySize + vSize, uSize)
        val yuv = YuvImage(nv21, ImageFormat.NV21, width, height, null)
        val out = ByteArrayOutputStream()
        yuv.compressToJpeg(Rect(0, 0, width, height), quality, out)
        return out.toByteArray()
    }
}

/**
 * Two-stage capture object held between
 * [ARCaptureSession.acquireRawFrame] and the per-frame decision.
 * Owns the underlying ARCore [Image] until [close] (the standard
 * Kotlin `use` pattern). Call [encodeJpeg] to materialise the
 * compressed bytes — skip it on a dropped frame and the YUV→JPEG
 * cost (~10–15 ms on a 1080p frame) is avoided entirely.
 *
 * [pose] is the ARCore [Pose] (quaternion + translation), suitable
 * for cheap frame-to-frame velocity math. [poseMatrix] is the
 * column-major 4×4 the existing disk-write path wants — both are
 * exposed so the call site doesn't pay for the matrix conversion
 * on dropped frames.
 */
class RawFrame internal constructor(
    val image: Image,
    val pose: Pose,
    val intrinsics: Intrinsics,
    val timestampNs: Long,
    val trackingState: TrackingState,
    private val jpegQuality: Int,
) : AutoCloseable {
    val yBuffer: ByteBuffer get() = image.planes[0].buffer
    val yRowStride: Int get() = image.planes[0].rowStride
    val yPixelStride: Int get() = image.planes[0].pixelStride
    val width: Int get() = image.width
    val height: Int get() = image.height

    fun poseMatrix(): FloatArray = FloatArray(16).also { pose.toMatrix(it, 0) }

    fun encodeJpeg(session: ARCaptureSession): ByteArray =
        session.yuvToJpegInternal(image, jpegQuality)

    override fun close() {
        try { image.close() } catch (_: Exception) { /* idempotent */ }
    }
}

private inline fun <T : AutoCloseable, R> T.use(block: (T) -> R): R = try {
    block(this)
} finally {
    this.close()
}
