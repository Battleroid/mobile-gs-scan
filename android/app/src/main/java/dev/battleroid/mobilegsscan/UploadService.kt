package dev.battleroid.mobilegsscan

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.concurrent.ConcurrentHashMap

/**
 * Foreground service that runs [DraftUploader] outside the
 * activity lifecycle. Spawned by [DraftDetailActivity.startUpload]
 * via [start]; survives backgrounding / screen lock / dim / sleep
 * because the system keeps foreground-service processes around as
 * long as their persistent notification is up.
 *
 * Why a foreground service rather than WorkManager:
 *   * WorkManager survives process death too, but its built-in
 *     retry semantics aren't useful here — the multipart batch
 *     endpoints aren't idempotent (re-sending a batch would
 *     append duplicates), so resume-after-process-kill would
 *     require server-side batch acknowledgement work that
 *     wasn't part of this slice. Filed as a follow-up.
 *   * A foreground service with a progress notification covers
 *     the 90 % case the user actually feels: lock the screen,
 *     put the phone down, walk away — the upload finishes.
 *
 * Single in-flight upload at a time. A second [start] while one
 * is running is dropped silently; the existing run continues.
 *
 * Progress + result fan-out: the service writes to per-draft
 * [MutableStateFlow]s in [Status]. [DraftDetailActivity] collects
 * from these to drive its existing progress bar; the home
 * screen could read the same map to show an in-flight chip
 * (not done in this PR, but the surface is there). Cleared
 * automatically on terminal state.
 *
 * After a successful upload the service hands the resulting
 * scene id off to [SceneWatcher] (application-scoped) and
 * [stopSelf]'s. The "splat ready" notification then becomes the
 * watcher's responsibility — keeps the foreground service short-
 * lived so the user doesn't have to look at a "do not dismiss"
 * notification for the duration of the worker's 5-25 minute
 * train run.
 */
class UploadService : Service() {

    /** Per-draft upload status — read by the draft detail UI. */
    sealed interface UploadState {
        data class Running(val sent: Int, val total: Int) : UploadState
        data class Done(val captureId: String, val sceneId: String?) : UploadState
        data class Failed(val reason: String) : UploadState
    }

    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_DRAFT_ID = "draft_id"
        private const val ACTION_START = "start"
        private const val ACTION_CANCEL = "cancel"
        private const val NOTIF_ID_PROGRESS = 10_001
        private const val NOTIF_ID_PROCESSING_BASE = 11_000

        // App-scoped status map, indexed by draft id. Lives as
        // long as the process — long enough for any UI that
        // wants to render upload state. Activities should not
        // mutate this directly.
        private val statesInternal =
            ConcurrentHashMap<String, MutableStateFlow<UploadState?>>()

        fun stateFlow(draftId: String): StateFlow<UploadState?> =
            statesInternal.getOrPut(draftId) {
                MutableStateFlow(null)
            }.asStateFlow()

        fun start(ctx: Context, baseUrl: String, draftId: String) {
            // Seed the state flow so the UI sees Running(0, 0)
            // before the service has bound — avoids the half-
            // second blank gap between "Upload Now" tap and the
            // service's first onProgress callback.
            statesInternal.getOrPut(draftId) { MutableStateFlow(null) }
                .value = UploadState.Running(sent = 0, total = 0)
            val i = Intent(ctx, UploadService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_BASE_URL, baseUrl)
                putExtra(EXTRA_DRAFT_ID, draftId)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i)
            } else {
                ctx.startService(i)
            }
        }

        fun cancel(ctx: Context, draftId: String) {
            val i = Intent(ctx, UploadService::class.java).apply {
                action = ACTION_CANCEL
                putExtra(EXTRA_DRAFT_ID, draftId)
            }
            ctx.startService(i)
        }

        private fun setState(draftId: String, s: UploadState?) {
            statesInternal.getOrPut(draftId) { MutableStateFlow(null) }.value = s
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    private var activeDraftId: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        if (action == ACTION_CANCEL) {
            // Best-effort: cancel the in-flight job only if it
            // matches the draft id the user asked to cancel. A
            // cancel intent for a *different* draft (stale
            // notification, race with a queued retry, etc.) must
            // NOT tear down the active upload — calling
            // ``stopSelf`` unconditionally here cancels the
            // service scope in ``onDestroy`` and aborts whatever
            // run was in flight. We only stop the service if
            // there is no active run to protect.
            val draftId = intent.getStringExtra(EXTRA_DRAFT_ID).orEmpty()
            if (draftId.isNotEmpty() && draftId == activeDraftId) {
                job?.cancel()
                setState(draftId, UploadState.Failed("canceled by user"))
                stopSelf()
            } else if (activeDraftId == null) {
                stopSelf()
            }
            // else: keep the service alive — the active upload
            // is unrelated to this cancel intent.
            return START_NOT_STICKY
        }

        val baseUrl = intent?.getStringExtra(EXTRA_BASE_URL).orEmpty()
        val draftId = intent?.getStringExtra(EXTRA_DRAFT_ID).orEmpty()
        if (baseUrl.isBlank() || draftId.isBlank()) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (job?.isActive == true) {
            // Don't run a second upload in parallel — the
            // foreground service contract is one progress
            // notification per service instance. The new request
            // is rejected; surface that to the UI as a terminal
            // Failed state so the per-draft progress bar (which
            // ``start`` seeded with Running(0, 0) before
            // entering the service) doesn't get stuck pretending
            // an upload is happening.
            if (draftId != activeDraftId) {
                setState(
                    draftId,
                    UploadState.Failed(
                        "another upload is already running; try again when it finishes",
                    ),
                )
            }
            return START_NOT_STICKY
        }

        activeDraftId = draftId
        startForegroundCompat(buildProgressNotification(0, 0, draftId, baseUrl))
        job = scope.launch {
            runUpload(baseUrl = baseUrl, draftId = draftId)
            stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun startForegroundCompat(notif: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIF_ID_PROGRESS,
                notif,
                // dataSync — the closest matching type for a
                // long HTTP transfer that isn't location, media
                // playback, or call routing.
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIF_ID_PROGRESS, notif)
        }
    }

    private suspend fun runUpload(baseUrl: String, draftId: String) {
        val draft = DraftStore.openDraft(this, draftId)
        if (draft == null) {
            setState(draftId, UploadState.Failed("draft no longer exists"))
            return
        }
        val client = StudioClient(baseUrl)
        val uploader = DraftUploader(this, baseUrl, client)
        val nm = getSystemService(NotificationManager::class.java)
        val result = try {
            uploader.upload(scope, draft) { sent, total ->
                setState(draftId, UploadState.Running(sent, total))
                nm?.notify(
                    NOTIF_ID_PROGRESS,
                    buildProgressNotification(sent, total, draftId, baseUrl),
                )
            }
        } catch (e: Exception) {
            DraftUploader.Result.Failed(e.message ?: "unknown")
        }

        when (result) {
            is DraftUploader.Result.Ok -> {
                setState(
                    draftId,
                    UploadState.Done(
                        captureId = result.captureId,
                        sceneId = result.sceneId,
                    ),
                )
                postProcessingStartedNotification(
                    captureId = result.captureId,
                    sceneId = result.sceneId,
                    captureName = draft.meta.name.orEmpty(),
                    baseUrl = baseUrl,
                )
                // Hand the scene id to the application-scoped
                // watcher so the "splat ready" notification can
                // fire once the worker pipeline finishes. The FG
                // service itself ends here.
                result.sceneId?.let { sid ->
                    SceneWatcher.watch(
                        ctx = applicationContext,
                        baseUrl = baseUrl,
                        sceneId = sid,
                        captureId = result.captureId,
                        captureName = draft.meta.name.orEmpty(),
                    )
                }
            }
            is DraftUploader.Result.Failed -> {
                setState(draftId, UploadState.Failed(result.reason))
                postUploadFailedNotification(
                    draftId = draftId,
                    reason = result.reason,
                    baseUrl = baseUrl,
                )
            }
        }
    }

    private fun buildProgressNotification(
        sent: Int,
        total: Int,
        draftId: String,
        baseUrl: String,
    ): Notification {
        val title = if (total > 0) "Uploading $sent / $total frames"
                    else "Uploading capture…"
        val tap = Intent(this, DraftDetailActivity::class.java).apply {
            putExtra(DraftDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(DraftDetailActivity.EXTRA_DRAFT_ID, draftId)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        val pi = PendingIntent.getActivity(
            this, draftId.hashCode(), tap,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val cancelIntent = Intent(this, UploadService::class.java).apply {
            action = ACTION_CANCEL
            putExtra(EXTRA_DRAFT_ID, draftId)
        }
        val cancelPi = PendingIntent.getService(
            this, draftId.hashCode() + 1, cancelIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = NotificationCompat.Builder(
            this, NotificationChannels.CHANNEL_UPLOAD,
        )
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle(title)
            .setContentIntent(pi)
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                "Cancel",
                cancelPi,
            )
            .setOngoing(true)
            .setOnlyAlertOnce(true)
        if (total > 0) {
            builder.setProgress(total, sent, false)
        } else {
            builder.setProgress(0, 0, true)
        }
        return builder.build()
    }

    private fun postProcessingStartedNotification(
        captureId: String,
        sceneId: String?,
        captureName: String,
        baseUrl: String,
    ) {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        val intent = Intent(this, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, captureId)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, captureName)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }
        val pi = PendingIntent.getActivity(
            this, captureId.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notif = NotificationCompat.Builder(
            this, NotificationChannels.CHANNEL_PROCESSING,
        )
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setContentTitle("Upload complete")
            .setContentText(
                if (captureName.isBlank()) "Processing started · tap for details"
                else "$captureName · processing started"
            )
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        // Per-capture id so two concurrent uploads don't replace
        // each other's "processing started" toast.
        nm.notify(NOTIF_ID_PROCESSING_BASE + captureId.hashCode(), notif)
    }

    private fun postUploadFailedNotification(
        draftId: String,
        reason: String,
        baseUrl: String,
    ) {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        val intent = Intent(this, DraftDetailActivity::class.java).apply {
            putExtra(DraftDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(DraftDetailActivity.EXTRA_DRAFT_ID, draftId)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        val pi = PendingIntent.getActivity(
            this, draftId.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notif = NotificationCompat.Builder(
            this, NotificationChannels.CHANNEL_PROCESSING,
        )
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("Upload failed")
            .setContentText(reason.take(120))
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        nm.notify(NOTIF_ID_PROCESSING_BASE + draftId.hashCode(), notif)
    }
}

