package dev.battleroid.mobilegsscan

import android.content.Context
import com.google.ar.core.CameraConfig
import com.google.ar.core.CameraConfigFilter
import com.google.ar.core.Session
import java.util.EnumSet
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * One supported ARCore CameraConfig described by stable display
 * fields. ``key`` is the same canonical string used in
 * [ServerConfig.cameraConfigKey] and as the lookup id by
 * [resolveCameraConfig]; ``label`` is a one-line UI string the
 * Settings chip renders ("1920×1080 · 30 fps").
 */
data class CameraConfigOption(
    val key: String,
    val label: String,
    val width: Int,
    val height: Int,
    val fps: Int,
)

/**
 * Briefly create an ARCore [Session] and enumerate the device's
 * supported back-facing camera configs. Returns the list once, then
 * closes the session.
 *
 * Throws on any failure — missing ARCore install, camera permission
 * not granted, or device with no back camera. The caller (Settings
 * activity) wraps this in a try/catch and falls back to the legacy
 * freeform fps slider on error, with an inline explainer.
 *
 * Filtering choices:
 *   * Back camera only (``FacingDirection.BACK``) — the AR capture
 *     flow streams the rear camera; the front-facing configs would
 *     show up but never get used.
 *   * Depth sensor not required (``DO_NOT_USE``) — the capture
 *     pipeline doesn't read depth, and requiring it hides configs
 *     on phones whose RGB camera supports more resolutions than
 *     their depth sensor does.
 */
suspend fun probeCameraConfigs(context: Context): List<CameraConfigOption> =
    withContext(Dispatchers.IO) {
        val session = Session(context)
        try {
            val filter = CameraConfigFilter(session)
                .setFacingDirection(CameraConfig.FacingDirection.BACK)
                .setDepthSensorUsage(
                    EnumSet.of(CameraConfig.DepthSensorUsage.DO_NOT_USE),
                )
            session.getSupportedCameraConfigs(filter)
                .map(::cameraConfigOption)
                // Stable order so the chip row reads predictably
                // across runs. Sort by pixel-count descending so
                // the highest-resolution options surface first;
                // ties broken by fps descending so 1080p60 outranks
                // 1080p30.
                .sortedWith(
                    compareByDescending<CameraConfigOption> { it.width * it.height }
                        .thenByDescending { it.fps },
                )
                // De-dup on the canonical key — some devices expose
                // multiple physical configs that map to the same
                // visible (w, h, fps) tuple (e.g. different image
                // formats). Keep the first.
                .distinctBy { it.key }
        } finally {
            session.close()
        }
    }

/**
 * Look up the ARCore CameraConfig matching the user's selected
 * preset key. Returns null when the key is [ServerConfig.CAMERA_CONFIG_CUSTOM]
 * (the user explicitly wants the system default), when no config
 * matches (the device's supported set changed since the user
 * picked), or when ``key`` is empty.
 *
 * Called from [ARCaptureSession] on session construction: a
 * non-null return is passed to ``session.setCameraConfig`` before
 * the first ``configure`` call.
 */
fun resolveCameraConfig(session: Session, key: String): CameraConfig? {
    if (key.isBlank() || key == ServerConfig.CAMERA_CONFIG_CUSTOM) return null
    val filter = CameraConfigFilter(session)
        .setFacingDirection(CameraConfig.FacingDirection.BACK)
        .setDepthSensorUsage(
            EnumSet.of(CameraConfig.DepthSensorUsage.DO_NOT_USE),
        )
    return session.getSupportedCameraConfigs(filter)
        .firstOrNull { cameraConfigOption(it).key == key }
}

private fun cameraConfigOption(cfg: CameraConfig): CameraConfigOption {
    val w = cfg.imageSize.width
    val h = cfg.imageSize.height
    // CameraConfig exposes an fps RANGE; treat the upper bound as
    // "the fps this preset advertises" — that's what a user picking
    // a "60 fps" chip actually wants. ARCore will clamp delivery to
    // whatever the hardware can hit at runtime.
    val fps = cfg.fpsRange.upper
    return CameraConfigOption(
        key = "${w}x${h}@${fps}",
        label = "${w}×${h} · ${fps} fps",
        width = w,
        height = h,
        fps = fps,
    )
}
