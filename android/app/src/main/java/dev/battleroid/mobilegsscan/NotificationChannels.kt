package dev.battleroid.mobilegsscan

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build

/**
 * Notification channels for the upload + processing surface.
 *
 * Three channels — split because their importance + user
 * expectations differ:
 *
 * * [CHANNEL_UPLOAD] — low importance, no sound, no vibration. The
 *   foreground-service progress notification while [UploadService]
 *   is pumping JPEG batches over HTTP. Persistent (the user can't
 *   swipe it away) so Android keeps the service alive across
 *   backgrounding / screen lock / dim / sleep.
 * * [CHANNEL_PROCESSING] — default importance, one-shot. Fires
 *   when the upload finishes and the server has accepted the
 *   capture; reassures the user the bytes arrived. Tap routes
 *   to the per-capture detail screen.
 * * [CHANNEL_READY] — default importance, one-shot. Fires when
 *   the worker pipeline finishes producing a viewable splat
 *   (signalled by ``scene.completed`` over the per-scene WS).
 *   Tap routes to [SplatViewerActivity]. Channel separate from
 *   processing-started so users can mute "we got your upload"
 *   pings without losing the "your splat is ready" alert.
 *
 * Channels are registered idempotently from [App.onCreate]; the
 * platform deduplicates by channel id so calling multiple times
 * is safe.
 */
object NotificationChannels {
    const val CHANNEL_UPLOAD = "upload_progress"
    const val CHANNEL_PROCESSING = "processing_started"
    const val CHANNEL_READY = "splat_ready"

    fun register(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return

        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_UPLOAD,
                "Uploads",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Live progress while a capture is uploading."
                setShowBadge(false)
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_PROCESSING,
                "Upload complete",
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply {
                description = "Fires when the studio receives a capture and starts processing it."
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_READY,
                "Splat ready",
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply {
                description = "Fires when a capture is fully processed and ready to view."
            },
        )
    }
}
