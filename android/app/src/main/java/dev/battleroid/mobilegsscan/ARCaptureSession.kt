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
import com.google.ar.core.PointCloud
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.NotYetAvailableException
import java.io.ByteArrayOutputStream

/**
 * Thin wrapper over an ARCore [Session] that:
 *   - configures the session for fastest CPU image access (the GPU
 *     image pipeline is too involved for the current overlay; we
 *     keep the overlay minimal and read CPU-side YUV instead).
 *   - opportunistically enables ARCore's depth mode at session
 *     create time when the device supports it. The Phase 3
 *     [DepthMeshRenderer] consumes this; the Phase 1
 *     [CoverageRenderer] doesn't care either way.
 *   - exposes [update] which advances the session by one frame and
 *     returns the resulting [Frame], so callers can run their own
 *     rendering against ARCore's camera-feed texture.
 *   - exposes [pollFrameData] which extracts the most recent tracked
 *     pose + intrinsics + JPEG-encoded RGB image from a Frame,
 *     throttled to a target frame rate.
 *   - exposes [viewMatrix] / [projectionMatrix] / [acquirePointCloud]
 *     / [acquireDepthImage16] so overlay renderers can project and
 *     color world-space geometry without reaching into the
 *     underlying [Session].
 *
 * Designed to be driven from the `GLSurfaceView` render thread.
 */
class ARCaptureSession(
    context: Context,
    private val targetIntervalMs: Long = 200, // 5 fps default
    private val jpegQuality: Int = 85,
) {

    /**
     * True when ARCore reports the device can compute a depth image
     * (Pixel-class hardware + recent Play Services for AR). Cached
     * at session-create time. Phase 3 callers gate their depth
     * acquisition on this — the alternative is repeated
     * NotYetAvailableException churn per frame.
     */
    val depthSupported: Boolean

    private val session: Session = Session(context).also { s ->
        val supported = s.isDepthModeSupported(Config.DepthMode.AUTOMATIC)
        depthSupported = supported
        val cfg = Config(s).apply {
            focusMode = Config.FocusMode.AUTO
            updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
            lightEstimationMode = Config.LightEstimationMode.DISABLED
            planeFindingMode = Config.PlaneFindingMode.HORIZONTAL_AND_VERTICAL
            depthMode = if (supported) Config.DepthMode.AUTOMATIC else Config.DepthMode.DISABLED
        }
        s.configure(cfg)
    }
    private var lastEmitMs: Long = 0
    private var frameIndex: Int = 0

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
     * Column-major 4x4 cam-to-world pose for [frame]. Phase 3's
     * [DepthMeshRenderer] needs this to lift each depth pixel from
     * the camera's local frame into world space so the mesh stays
     * put as the camera moves.
     */
    fun cameraPose(frame: Frame): FloatArray =
        FloatArray(16).also { frame.camera.pose.toMatrix(it, 0) }

    /**
     * Color-camera intrinsics for [frame] in pixel units. Already
     * the same call shape `pollFrameData` uses internally; surfaced
     * here so overlay renderers can ask without going through the
     * frame-streaming path.
     */
    fun colorIntrinsics(frame: Frame): Intrinsics =
        readIntrinsics(frame.camera.imageIntrinsics)

    /**
     * Acquire the tracked feature point cloud from [frame]. The
     * returned [PointCloud] is `Closeable` — caller must close it
     * (idiomatic Kotlin: `acquirePointCloud(frame).use { ... }`).
     */
    fun acquirePointCloud(frame: Frame): PointCloud =
        frame.acquirePointCloud()

    /**
     * Acquire the 16-bit depth image (uint16 millimeters,
     * little-endian) for [frame]. Returns null when ARCore hasn't
     * computed a depth frame yet — common for the first few frames
     * after session start, and intermittently while tracking
     * stabilizes. The returned [Image] is `Closeable` — caller
     * must close it.
     *
     * Throws [IllegalStateException] if the session was started
     * without depth support; gate on [depthSupported] first.
     */
    fun acquireDepthImage16(frame: Frame): Image? = try {
        frame.acquireDepthImage16Bits()
    } catch (e: NotYetAvailableException) {
        null
    }

    /**
     * Extract pose + intrinsics + JPEG from a Frame returned by
     * [update], if and only if:
     *   - ARCore is currently TRACKING (so the pose is meaningful)
     *   - enough time has elapsed since the last emit to honour the
     *     [targetIntervalMs] rate limit
     *   - acquireCameraImage actually has a frame ready (NotYet on
     *     the first few calls is normal).
     *
     * Returns null in any of those cases.
     */
    fun pollFrameData(frame: Frame): CapturedFrame? {
        val camera: Camera = frame.camera
        if (camera.trackingState != TrackingState.TRACKING) return null

        val now = frame.timestamp / 1_000_000L
        if (now - lastEmitMs < targetIntervalMs) return null

        val jpeg = encodeFrameJpeg(frame) ?: return null
        val intrinsics = readIntrinsics(camera.imageIntrinsics)
        val pose = FloatArray(16).also { camera.pose.toMatrix(it, 0) }

        lastEmitMs = now
        val idx = frameIndex++
        return CapturedFrame(idx = idx, jpeg = jpeg, pose = pose, intrinsics = intrinsics)
    }

    private fun encodeFrameJpeg(frame: Frame): ByteArray? {
        val image: Image = try {
            frame.acquireCameraImage()
        } catch (e: NotYetAvailableException) {
            return null
        }
        return image.use { yuvToJpeg(it, jpegQuality) }
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

/** One frame ready for streaming. [pose] is a column-major 4x4 in world space. */
data class CapturedFrame(
    val idx: Int,
    val jpeg: ByteArray,
    val pose: FloatArray,
    val intrinsics: Intrinsics,
)

private inline fun <T : AutoCloseable, R> T.use(block: (T) -> R): R = try {
    block(this)
} finally {
    this.close()
}
