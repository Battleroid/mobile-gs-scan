package dev.battleroid.mobilegsscan

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

/**
 * Persists per-app preferences:
 *   - studio URL (which server to talk to)
 *   - capture rate + JPEG quality (frame-streaming knobs)
 *   - training-fidelity preset (per-capture splatfacto iter count)
 *   - coverage-overlay opacity (translucency of the AR dots)
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
    private const val KEY_OVERLAY_ALPHA = "overlay_alpha"
    private const val KEY_CAMERA_CONFIG = "camera_config"
    private const val KEY_FRAME_FILTER_ENABLED = "frame_filter_enabled"
    private const val KEY_FRAME_FILTER_BLUR = "frame_filter_blur"
    private const val KEY_FRAME_FILTER_MOTION = "frame_filter_motion"
    private const val KEY_FRAME_FILTER_EXPOSURE = "frame_filter_exposure"
    private const val KEY_CAPTURE_PROFILE = "capture_profile"

    /** Sentinel value for ``cameraConfigKey`` meaning "don't apply a
     *  specific ARCore CameraConfig; let the system default win, and
     *  honor the user's freeform fps slider instead". Stored verbatim
     *  in SharedPreferences so a future format change can be detected
     *  without a migration. */
    const val CAMERA_CONFIG_CUSTOM = "custom"

    // Capture-rate defaults. The previous hardcoded
    // ``targetIntervalMs = 200`` (5 fps) in ARCaptureSession was too
    // sparse for splat-quality coverage and made the WS counter look
    // stuck. 10 fps is a reasonable default — enough motion
    // resolution for splatting, not enough to saturate phone-side
    // YUV-to-JPEG encoding on a Pixel-class device.
    const val DEFAULT_FPS = 10
    const val MIN_FPS = 1
    // Raised from 30 to 60 so users on phones whose ARCore camera
    // delivers 60 fps native can stream at the higher rate. The
    // captureIntervalMs floor (16 ms below) is the matching gate
    // on the renderer side. If a device's ARCore CameraConfig
    // tops out at 30 fps (the common default), sampling at 60 fps
    // is harmless — ARCaptureSession just samples every frame it
    // gets, which can't exceed the camera's native rate.
    const val MAX_FPS = 60

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

    // Coverage-overlay opacity, persisted as integer percent so the
    // SeekBar maps 1:1. Floor at 20% so the user can't accidentally
    // make the overlay invisible and think it's broken; cap at 100%
    // so they can still get fully-opaque dots if they want them.
    const val DEFAULT_OVERLAY_ALPHA_PCT = 70
    const val MIN_OVERLAY_ALPHA_PCT = 20
    const val MAX_OVERLAY_ALPHA_PCT = 100

    // Frame-quality filter (see ``FrameQualityFilter``). Defaults
    // are tuned to drop *obviously* bad frames on a typical
    // handheld sweep without rejecting marginal ones — the goal is
    // "cleaner datasets out of the box" not "drop everything that
    // isn't perfect." Power users can tune via the Advanced
    // Settings section.
    const val DEFAULT_FRAME_FILTER_ENABLED = true
    // Absolute Laplacian-variance floor. ~80 is a reasonable
    // catch-all for handheld captures of textured scenes; soft-
    // contrast scenes (uniform walls) score naturally low and
    // the relative-EMA gate inside the filter handles them.
    const val DEFAULT_FRAME_FILTER_BLUR = 80
    const val MIN_FRAME_FILTER_BLUR = 20
    const val MAX_FRAME_FILTER_BLUR = 500
    // 0–100 strictness slider. Maps inside the filter to a
    // linear blend of (lin m/s, ang deg/s) thresholds:
    // 0 → very lenient (1.0 m/s, 70 deg/s), 100 → very strict
    // (0.1 m/s, 5 deg/s). Default 50 sits at the canonical
    // 0.35 m/s, 25 deg/s.
    const val DEFAULT_FRAME_FILTER_MOTION = 50
    const val MIN_FRAME_FILTER_MOTION = 0
    const val MAX_FRAME_FILTER_MOTION = 100
    // 0–100 strictness slider. Maps to ``exposureSigma``: 0 →
    // 5σ (very lenient), 100 → 1.5σ (very strict). Default 50
    // sits at the canonical 3σ.
    const val DEFAULT_FRAME_FILTER_EXPOSURE = 50
    const val MIN_FRAME_FILTER_EXPOSURE = 0
    const val MAX_FRAME_FILTER_EXPOSURE = 100

    // Capture-profile chips. A profile bundles fps + filter
    // strictness behind a one-tap selector. ``CUSTOM`` means the
    // user is mixing-and-matching via the underlying sliders;
    // selecting a named profile *snaps* the fps + filter sliders
    // to the matching triple. Profiles are detected on reload by
    // comparing the persisted values to the canonical triple
    // (allowing a small tolerance), falling back to ``CUSTOM``
    // when the triple doesn't match.
    const val CAPTURE_PROFILE_CUSTOM = "custom"
    const val CAPTURE_PROFILE_SMOOTH = "smooth"
    const val CAPTURE_PROFILE_BALANCED = "balanced"
    const val CAPTURE_PROFILE_SPARSE = "sparse"

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

    fun coverageOverlayAlphaPct(ctx: Context): Int =
        prefs(ctx)
            .getInt(KEY_OVERLAY_ALPHA, DEFAULT_OVERLAY_ALPHA_PCT)
            .coerceIn(MIN_OVERLAY_ALPHA_PCT, MAX_OVERLAY_ALPHA_PCT)

    fun setCoverageOverlayAlphaPct(ctx: Context, pct: Int) {
        prefs(ctx).edit {
            putInt(
                KEY_OVERLAY_ALPHA,
                pct.coerceIn(MIN_OVERLAY_ALPHA_PCT, MAX_OVERLAY_ALPHA_PCT),
            )
        }
    }

    /** Convenience for CoverageRenderer.setAlpha — same value, [0, 1]. */
    fun coverageOverlayAlphaFloat(ctx: Context): Float =
        coverageOverlayAlphaPct(ctx) / 100f

    /** ARCore camera-config preset id. Format ``<width>x<height>@<fps>``
     *  (e.g. ``1920x1080@30``) when one of the device-supported
     *  presets is selected, or [CAMERA_CONFIG_CUSTOM] when the user
     *  has opted to drive the capture rate freeform via the fps
     *  slider. Defaults to [CAMERA_CONFIG_CUSTOM] so installs that
     *  predate the preset chip keep their existing behaviour. */
    fun cameraConfigKey(ctx: Context): String =
        prefs(ctx).getString(KEY_CAMERA_CONFIG, CAMERA_CONFIG_CUSTOM)
            ?: CAMERA_CONFIG_CUSTOM

    fun setCameraConfigKey(ctx: Context, key: String) {
        prefs(ctx).edit { putString(KEY_CAMERA_CONFIG, key) }
    }

    fun frameFilterEnabled(ctx: Context): Boolean =
        prefs(ctx).getBoolean(KEY_FRAME_FILTER_ENABLED, DEFAULT_FRAME_FILTER_ENABLED)

    fun setFrameFilterEnabled(ctx: Context, enabled: Boolean) {
        prefs(ctx).edit { putBoolean(KEY_FRAME_FILTER_ENABLED, enabled) }
    }

    fun frameFilterBlur(ctx: Context): Int =
        prefs(ctx)
            .getInt(KEY_FRAME_FILTER_BLUR, DEFAULT_FRAME_FILTER_BLUR)
            .coerceIn(MIN_FRAME_FILTER_BLUR, MAX_FRAME_FILTER_BLUR)

    fun setFrameFilterBlur(ctx: Context, value: Int) {
        prefs(ctx).edit {
            putInt(KEY_FRAME_FILTER_BLUR, value.coerceIn(MIN_FRAME_FILTER_BLUR, MAX_FRAME_FILTER_BLUR))
        }
    }

    fun frameFilterMotion(ctx: Context): Int =
        prefs(ctx)
            .getInt(KEY_FRAME_FILTER_MOTION, DEFAULT_FRAME_FILTER_MOTION)
            .coerceIn(MIN_FRAME_FILTER_MOTION, MAX_FRAME_FILTER_MOTION)

    fun setFrameFilterMotion(ctx: Context, value: Int) {
        prefs(ctx).edit {
            putInt(KEY_FRAME_FILTER_MOTION, value.coerceIn(MIN_FRAME_FILTER_MOTION, MAX_FRAME_FILTER_MOTION))
        }
    }

    fun frameFilterExposure(ctx: Context): Int =
        prefs(ctx)
            .getInt(KEY_FRAME_FILTER_EXPOSURE, DEFAULT_FRAME_FILTER_EXPOSURE)
            .coerceIn(MIN_FRAME_FILTER_EXPOSURE, MAX_FRAME_FILTER_EXPOSURE)

    fun setFrameFilterExposure(ctx: Context, value: Int) {
        prefs(ctx).edit {
            putInt(KEY_FRAME_FILTER_EXPOSURE, value.coerceIn(MIN_FRAME_FILTER_EXPOSURE, MAX_FRAME_FILTER_EXPOSURE))
        }
    }

    /** Translate the user-facing motion-strictness slider (0–100)
     *  to ``FrameQualityFilter.Config`` thresholds.
     *
     *  Piecewise-linear with the *default* slider value (50)
     *  pegged at the canonical mid threshold the Plan-agent
     *  design specified — 0.35 m/s linear and 25 deg/s angular.
     *  Plain (0, 1.0) → (100, 0.1) interpolation would put 50
     *  at 0.55 m/s, which is much too lenient — handheld shake
     *  on a slow pan often exceeds 0.4 m/s and would land in
     *  the dataset under that mapping. Two-segment math keeps
     *  the curve sane at both endpoints + at the default. */
    fun frameFilterLinVelMax(motionStrictness: Int): Double {
        val t = motionStrictness.coerceIn(0, 100) / 100.0
        // (0, 1.0) → (0.5, 0.35) → (1, 0.1)
        return if (t <= 0.5) 1.0 - 1.30 * t
        else 0.35 - 0.50 * (t - 0.5)
    }

    fun frameFilterAngVelMaxDeg(motionStrictness: Int): Double {
        val t = motionStrictness.coerceIn(0, 100) / 100.0
        // (0, 70) → (0.5, 25) → (1, 5)
        return if (t <= 0.5) 70.0 - 90.0 * t
        else 25.0 - 40.0 * (t - 0.5)
    }

    fun frameFilterExposureSigma(exposureStrictness: Int): Double {
        val t = exposureStrictness.coerceIn(0, 100) / 100.0
        // (0, 5.0) → (0.5, 3.0) → (1, 1.5). Lower sigma is
        // stricter — the default 50 lands at the 3σ rule the
        // filter docstring cites as canonical.
        return if (t <= 0.5) 5.0 - 4.0 * t
        else 3.0 - 3.0 * (t - 0.5)
    }

    /** Persisted capture-profile selection. See the constants
     *  ``CAPTURE_PROFILE_*``. Default is CUSTOM — i.e. let the
     *  underlying sliders drive, no automatic snap. */
    fun captureProfile(ctx: Context): String =
        prefs(ctx).getString(KEY_CAPTURE_PROFILE, CAPTURE_PROFILE_CUSTOM)
            ?: CAPTURE_PROFILE_CUSTOM

    fun setCaptureProfile(ctx: Context, profile: String) {
        prefs(ctx).edit { putString(KEY_CAPTURE_PROFILE, profile) }
    }

    /**
     * Translate the user-facing "fps" prefs to the per-frame interval
     * the ARCaptureSession rate-limit reads. Floor at 16 ms (≈60 fps)
     * matches typical 60 Hz display refresh; the previous 33 ms floor
     * capped the effective rate at ~30 fps regardless of the user's
     * setting. Phones with 90 / 120 Hz displays still see 60 fps as
     * the cap — that's a deliberate fp32-capture-overhead choice, not
     * a display-refresh limit; if we ever want >60 we'd also need to
     * verify ARCore CameraConfig actually delivers it.
     */
    fun captureIntervalMs(ctx: Context): Long {
        val fps = captureFps(ctx)
        return (1000L / fps).coerceAtLeast(16L)
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
