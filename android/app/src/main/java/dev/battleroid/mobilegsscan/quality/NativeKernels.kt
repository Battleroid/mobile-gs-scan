package dev.battleroid.mobilegsscan.quality

import java.nio.ByteBuffer

/**
 * JNI bridge to the single-pass quality kernel built from
 * ``app/src/main/cpp/frame_quality.cpp``. One static method —
 * [analyze] — covers everything the frame filter needs:
 *
 *   * variance of Laplacian (sharpness proxy)
 *   * Y plane mean + variance (exposure tracking)
 *   * dark-clip and bright-clip percentages (crushed / blown
 *     detection)
 *
 * Inputs are the Y plane of an ARCore NV21 image taken directly
 * from ``android.media.Image.Plane.getBuffer()``. The buffer must
 * be a direct ``ByteBuffer`` — ARCore's plane buffers are direct
 * by spec, so this holds for our call site. If the platform ever
 * hands us a heap buffer, [analyze] returns five zeros and the
 * caller logs a one-shot "kernel unavailable" warning.
 *
 * Output is a five-element ``DoubleArray`` indexed by
 * [INDEX_LAPLACIAN_VARIANCE] etc. — the JNI surface stays a
 * single array so each frame's evaluation costs one
 * cross-language hop. The companion [Stats] data class wraps it
 * for the call site.
 */
object NativeKernels {
    const val INDEX_LAPLACIAN_VARIANCE = 0
    const val INDEX_Y_MEAN = 1
    const val INDEX_Y_VARIANCE = 2
    const val INDEX_DARK_CLIP_PCT = 3
    const val INDEX_BRIGHT_CLIP_PCT = 4

    @Volatile
    private var available: Boolean = false

    init {
        try {
            System.loadLibrary("pebble_native")
            available = true
        } catch (t: UnsatisfiedLinkError) {
            // Tests / pre-NDK builds — analyze returns null and
            // the filter degrades to accept-everything. We don't
            // crash the app on a missing .so; capture still
            // works, just without the kernel-driven gates.
            available = false
        }
    }

    fun isAvailable(): Boolean = available

    /**
     * @return a [DoubleArray] of length 5 (see ``INDEX_*``) or
     * ``null`` if the native library failed to load.
     */
    fun analyze(
        yBuffer: ByteBuffer,
        width: Int,
        height: Int,
        rowStride: Int,
        pixelStride: Int,
    ): DoubleArray? {
        if (!available) return null
        return analyze0(yBuffer, width, height, rowStride, pixelStride)
    }

    @JvmStatic
    private external fun analyze0(
        yBuffer: ByteBuffer,
        width: Int,
        height: Int,
        rowStride: Int,
        pixelStride: Int,
    ): DoubleArray

    /** Typed view over the array layout. Constructed by the
     *  filter; never crosses the JNI boundary itself. */
    data class Stats(
        val laplacianVariance: Double,
        val yMean: Double,
        val yVariance: Double,
        val darkClipPct: Double,
        val brightClipPct: Double,
    ) {
        companion object {
            fun fromArray(arr: DoubleArray): Stats = Stats(
                laplacianVariance = arr[INDEX_LAPLACIAN_VARIANCE],
                yMean = arr[INDEX_Y_MEAN],
                yVariance = arr[INDEX_Y_VARIANCE],
                darkClipPct = arr[INDEX_DARK_CLIP_PCT],
                brightClipPct = arr[INDEX_BRIGHT_CLIP_PCT],
            )
        }
    }
}
