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
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.NotYetAvailableException
import java.io.ByteArrayOutputStream

/**
 * Thin wrapper over an ARCore [Session] that:
 *   - configures the session for fastest CPU image access (the GPU
 *     image pipeline is too involved for PR #1's overlay; we keep the
 *     overlay minimal and read CPU-side YUV instead).
 *   - exposes [pollFrame] which returns the most recent tracked
 *     pose + intrinsics + JPEG-encoded RGB image, throttled to a
 *     target frame rate.
 *
 * Designed to be driven from the `GLSurfaceView` render thread.
 */
class ARCaptureSession(
    context: Context,
    private val targetIntervalMs: Long = 200, // 5 fps default
    private val jpegQuality: Int = 85,
) {

    private val session: Session = Session(context).apply {
        val cfg = Config(this).apply {
            focusMode = Config.FocusMode.AUTO
            updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
            lightEstimationMode = Config.LightEstimationMode.DISABLED
            planeFindingMode = Config.PlaneFindingMode.HORIZONTAL_AND_VERTICAL
        }
        configure(cfg)
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
     * Pull the next frame from ARCore, encode it to JPEG when enough
     * time has elapsed since the last emit, and hand the result back.
     * Returns null when ARCore is still warming up or we're being
     * rate-limited by [targetIntervalMs].
     */
    fun pollFrame(): CapturedFrame? {
        val frame: Frame = try {
            session.update()
        } catch (e: Exception) {
            return null
        }
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
