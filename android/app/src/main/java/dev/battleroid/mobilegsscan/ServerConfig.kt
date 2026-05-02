package dev.battleroid.mobilegsscan

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

/**
 * Persists per-app preferences:
 *   - studio URL (which server to talk to)
 *   - capture rate + JPEG quality (frame-streaming knobs)
 *   - training-fidelity preset (per-capture splatfacto iter count)
 *
 * Single-user, single-server — there's no list of saved servers,
 * just the most recent one.
 */
object ServerConfig {
    private const val PREFS = "studio"
    private const val KEY_URL = "studio_url"
    private const val KEY_FPS = "capture_fps"
    private const val KEY_JPEG_QUALITY = "capture_jpeg_quality"
    private const val KEY_TRAIN_ITERS = "train_iters"

    // Capture-rate defaults. The previous hardcoded
    // ``targetIntervalMs = 200`` (5 fps) in ARCaptureSession was too
    // sparse for splat-quality coverage and made the WS counter look
    // stuck. 10 fps is a reasonable default — enough motion
    // resolution for splatting, not enough to saturate phone-side
    // YUV-to-JPEG encoding on a Pixel-class device.
    const val DEFAULT_FPS = 10
    const val MIN_FPS = 1
    const val MAX_FPS = 30

    const val DEFAULT_JPEG_QUALITY = 85
    const val MIN_JPEG_QUALITY = 50
    const val MAX_JPEG_QUALITY = 100

    // Training-fidelity presets. Splatfacto's training cost scales
    // roughly linearly with iters; on a 4090 that's ~3 min for 5k,
    // ~10 min for 15k, ~25 min for 30k. Standard (15000) matches
    // the previous server-side default in GS_TRAIN_ITERS; we use
    // it as the app-side default too so behaviour is unchanged for
    // users who don't touch the preset.
    const val TRAIN_ITERS_LOW = 5_000
    const val TRAIN_ITERS_STANDARD = 15_000
    const val TRAIN_ITERS_HIGH = 30_000
    const val DEFAULT_TRAIN_ITERS = TRAIN_ITERS_STANDARD

    fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun studioUrl(ctx: Context): String? =
        prefs(ctx).getString(KEY_URL, null)?.takeIf { it.isNotBlank() }

    fun setStudioUrl(ctx: Context, url: String) {
        prefs(ctx).edit { putString(KEY_URL, normalize(url)) }
    }

    fun captureFps(ctx: Context): Int =
        prefs(ctx)
            .getInt(KEY_FPS, DEFAULT_FPS)
            .coerceIn(MIN_FPS, MAX_FPS)

    fun setCaptureFps(ctx: Context, fps: Int) {
        prefs(ctx).edit { putInt(KEY_FPS, fps.coerceIn(MIN_FPS, MAX_FPS)) }
    }

    fun captureJpegQuality(ctx: Context): Int =
        prefs(ctx)
            .getInt(KEY_JPEG_QUALITY, DEFAULT_JPEG_QUALITY)
            .coerceIn(MIN_JPEG_QUALITY, MAX_JPEG_QUALITY)

    fun setCaptureJpegQuality(ctx: Context, q: Int) {
        prefs(ctx).edit {
            putInt(KEY_JPEG_QUALITY, q.coerceIn(MIN_JPEG_QUALITY, MAX_JPEG_QUALITY))
        }
    }

    fun captureTrainIters(ctx: Context): Int =
        prefs(ctx)
            .getInt(KEY_TRAIN_ITERS, DEFAULT_TRAIN_ITERS)
            .coerceAtLeast(1)

    fun setCaptureTrainIters(ctx: Context, iters: Int) {
        prefs(ctx).edit { putInt(KEY_TRAIN_ITERS, iters.coerceAtLeast(1)) }
    }

    /**
     * Translate the user-facing "fps" prefs to the per-frame interval
     * the ARCaptureSession rate-limit reads. Floor at 33ms to avoid
     * the renderer fighting itself when the display refresh is also
     * ~30 Hz.
     */
    fun captureIntervalMs(ctx: Context): Long {
        val fps = captureFps(ctx)
        return (1000L / fps).coerceAtLeast(33L)
    }

    /**
     * Normalize a user-typed studio URL.
     *
     *   - trim whitespace
     *   - drop a trailing `/`
     *   - prepend `https://` if no scheme is present (`make up-https`
     *     is the documented dev path; users mostly type bare IPs and
     *     hostnames). Users who genuinely want plain `http` can type
     *     it explicitly.
     *
     * Idempotent: a URL that already starts with `http://` or
     * `https://` is left alone.
     */
    fun normalize(raw: String): String {
        val trimmed = raw.trim().trimEnd('/')
        if (trimmed.isEmpty()) return ""
        val lower = trimmed.lowercase()
        if (lower.startsWith("http://") || lower.startsWith("https://")) {
            return trimmed
        }
        return "https://$trimmed"
    }
}
