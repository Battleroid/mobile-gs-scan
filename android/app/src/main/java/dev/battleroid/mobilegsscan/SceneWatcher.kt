package dev.battleroid.mobilegsscan

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlinx.coroutines.currentCoroutineContext

/**
 * Application-scoped watcher that holds one WebSocket per
 * in-flight scene id and posts a "splat ready" notification when
 * the worker pipeline fires ``scene.completed``.
 *
 * Lifecycle relative to the user's actions:
 *
 *   upload finished
 *     ↓  (UploadService hands sceneId off)
 *   watch(ctx, baseUrl, sceneId, captureName)
 *     ↓  open WS to /api/scenes/{id}/events
 *   waiting for "scene.completed" or snapshot.status == completed
 *     ↓
 *   post "splat ready" notification → remove from tracker
 *
 * Each watch is also persisted into [SharedPreferences] under
 * [PREFS_FILE] so a process restart can re-attach the WS via
 * [restorePending]. That covers the "user uploaded, backgrounded,
 * Android killed the process during a long mesh job" case.
 *
 * Server publishes the events covered here from:
 *   * worker/app/jobs/finalize.py — ``scene.completed`` /
 *     ``scene.canceled`` on terminal-state transitions.
 *   * worker/app/jobs/runner.py   — ``scene.failed`` when a
 *     pipeline-required job dies. We treat any of those as
 *     "stop watching"; failed/canceled fire a separate
 *     notification so the user knows the bytes landed but
 *     processing didn't.
 *
 * Auto-reconnect: the WS uses a 5 s backoff on disconnect (max
 * 60 s) so a flaky network — or a worker container restart — gets
 * recovered transparently. Stopping is driven by the terminal
 * state in the event stream, not by the WS lifecycle.
 */
object SceneWatcher {
    private const val PREFS_FILE = "scene_watch"
    // Stored as a JSON object: { "<sceneId>": { "baseUrl": ..., "name": ... } }
    private const val KEY_PENDING = "pending_v1"

    private val json = Json { ignoreUnknownKeys = true }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // no read timeout — WS is long-lived
        .build()

    private data class Pending(val baseUrl: String, val name: String)

    private val watching = ConcurrentHashMap<String, Job>()

    fun watch(ctx: Context, baseUrl: String, sceneId: String, captureName: String) {
        if (sceneId.isBlank() || baseUrl.isBlank()) return
        val app = ctx.applicationContext as App
        // Persist *before* we start the coroutine so a crash
        // mid-connect leaves a recoverable entry behind for
        // restorePending. Idempotent — repeated watches of the
        // same scene id collapse into a single WS.
        persist(ctx, sceneId, Pending(baseUrl, captureName))
        if (watching.containsKey(sceneId)) return
        val job = app.appScope.launch {
            try {
                runScene(ctx.applicationContext, baseUrl, sceneId, captureName)
            } finally {
                watching.remove(sceneId)
                clearPersisted(ctx, sceneId)
            }
        }
        watching[sceneId] = job
    }

    fun stop(ctx: Context, sceneId: String) {
        watching.remove(sceneId)?.cancel()
        clearPersisted(ctx, sceneId)
    }

    /**
     * Reads the persisted set and re-attaches watchers. Called
     * once from [App.onCreate].
     */
    fun restorePending(ctx: Context) {
        val pending = loadPersisted(ctx)
        for ((sceneId, p) in pending) {
            watch(ctx, p.baseUrl, sceneId, p.name)
        }
    }

    private suspend fun runScene(
        ctx: Context,
        baseUrl: String,
        sceneId: String,
        captureName: String,
    ) {
        var backoffMs = 5_000L
        while (true) {
            currentCoroutineContext().ensureActive()
            val terminal = try {
                connectOnce(baseUrl, sceneId)
            } catch (_: Exception) {
                null
            }
            when (terminal) {
                Terminal.Completed -> {
                    postReady(ctx, baseUrl, sceneId, captureName)
                    return
                }
                Terminal.Failed -> {
                    postFailed(ctx, baseUrl, sceneId, captureName)
                    return
                }
                Terminal.Canceled -> return
                null -> {
                    delay(backoffMs)
                    backoffMs = (backoffMs * 2).coerceAtMost(60_000L)
                }
            }
        }
    }

    private enum class Terminal { Completed, Failed, Canceled }

    /**
     * Opens one WS, returns the terminal state or null on
     * disconnect-without-terminal (caller retries).
     */
    private suspend fun connectOnce(baseUrl: String, sceneId: String): Terminal? {
        val wsUrl = baseUrl
            .replaceFirst("https://", "wss://")
            .replaceFirst("http://", "ws://")
            .trimEnd('/') + "/api/scenes/$sceneId/events"

        return suspendCancellableCoroutine { cont ->
            val req = Request.Builder().url(wsUrl).build()
            val ws = http.newWebSocket(req, object : WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) {
                    val terminal = parseTerminal(text)
                    if (terminal != null) {
                        webSocket.close(1000, null)
                        if (cont.isActive) cont.resume(terminal)
                    }
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    if (cont.isActive) cont.resume(null)
                }

                override fun onFailure(
                    webSocket: WebSocket,
                    t: Throwable,
                    response: Response?,
                ) {
                    response?.close()
                    if (cont.isActive) cont.resume(null)
                }
            })
            cont.invokeOnCancellation { ws.cancel() }
        }
    }

    private fun parseTerminal(text: String): Terminal? {
        val obj: JsonObject = try {
            json.parseToJsonElement(text).jsonObject
        } catch (_: Exception) {
            return null
        }
        val kind = (obj["kind"] as? JsonPrimitive)?.content.orEmpty()
        // Snapshot frames carry the current scene status; if the
        // user opens the app *after* the splat has finished, the
        // snapshot alone is enough to fire the notification (the
        // ``scene.completed`` event already fired and we missed
        // it). Otherwise we rely on the live event stream.
        if (kind == "snapshot") {
            val data = obj["data"] as? JsonObject ?: return null
            val status = (data["status"] as? JsonPrimitive)?.content.orEmpty()
            return when (status) {
                "completed" -> Terminal.Completed
                "failed" -> Terminal.Failed
                "canceled" -> Terminal.Canceled
                else -> null
            }
        }
        return when (kind) {
            "scene.completed" -> Terminal.Completed
            "scene.failed" -> Terminal.Failed
            "scene.canceled" -> Terminal.Canceled
            else -> null
        }
    }

    private fun postReady(
        ctx: Context,
        baseUrl: String,
        sceneId: String,
        captureName: String,
    ) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        // Tap routes through CaptureDetailActivity rather than
        // straight to the splat viewer: the viewer needs a
        // spz_url which isn't in the WS event payload — only the
        // status flip is. CaptureDetail re-fetches the scene and
        // surfaces the View button with the URL already resolved.
        val detail = Intent(ctx, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, captureName)
            // The detail screen accepts either capture_id or
            // scene_id; we only have the scene id from the WS
            // payload.
            putExtra("scene_id", sceneId)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }
        val pi = PendingIntent.getActivity(
            ctx, sceneId.hashCode(), detail,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notif = NotificationCompat.Builder(ctx, NotificationChannels.CHANNEL_READY)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("Splat ready")
            .setContentText(
                if (captureName.isBlank()) "Tap to view"
                else "$captureName · tap to view"
            )
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        nm.notify(NOTIF_ID_READY_BASE + sceneId.hashCode(), notif)
    }

    private fun postFailed(
        ctx: Context,
        baseUrl: String,
        sceneId: String,
        captureName: String,
    ) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        // Failed → route to CaptureDetail so the user can see
        // which job died and retry. The viewer would just show an
        // error.
        val detail = Intent(ctx, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            // We persist sceneId, not captureId — CaptureDetail
            // accepts either; the detail screen resolves by
            // scene_id when capture_id is absent. Pass both in
            // case the API surface tightens later.
            putExtra("scene_id", sceneId)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, captureName)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }
        val pi = PendingIntent.getActivity(
            ctx, sceneId.hashCode(), detail,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notif: Notification = NotificationCompat.Builder(
            ctx, NotificationChannels.CHANNEL_READY,
        )
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("Processing failed")
            .setContentText(
                if (captureName.isBlank()) "Tap to see details"
                else "$captureName · tap to see details"
            )
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        nm.notify(NOTIF_ID_READY_BASE + sceneId.hashCode(), notif)
    }

    private fun persist(ctx: Context, sceneId: String, p: Pending) {
        val prefs = ctx.applicationContext.getSharedPreferences(
            PREFS_FILE, Context.MODE_PRIVATE,
        )
        val current = loadPersisted(ctx).toMutableMap()
        current[sceneId] = p
        prefs.edit().putString(KEY_PENDING, encode(current)).apply()
    }

    private fun clearPersisted(ctx: Context, sceneId: String) {
        val prefs = ctx.applicationContext.getSharedPreferences(
            PREFS_FILE, Context.MODE_PRIVATE,
        )
        val current = loadPersisted(ctx).toMutableMap()
        if (current.remove(sceneId) != null) {
            prefs.edit().putString(KEY_PENDING, encode(current)).apply()
        }
    }

    private fun loadPersisted(ctx: Context): Map<String, Pending> {
        val prefs = ctx.applicationContext.getSharedPreferences(
            PREFS_FILE, Context.MODE_PRIVATE,
        )
        val raw = prefs.getString(KEY_PENDING, null) ?: return emptyMap()
        return try {
            val obj = json.parseToJsonElement(raw).jsonObject
            obj.entries.associate { (sceneId, el) ->
                val o = el.jsonObject
                sceneId to Pending(
                    baseUrl = o["baseUrl"]?.jsonPrimitive?.content.orEmpty(),
                    name = o["name"]?.jsonPrimitive?.content.orEmpty(),
                )
            }
        } catch (_: Exception) {
            emptyMap()
        }
    }

    private fun encode(map: Map<String, Pending>): String {
        // Tiny payload; hand-rolled JSON keeps us off another
        // serializer dependency.
        val sb = StringBuilder("{")
        map.entries.forEachIndexed { i, (k, v) ->
            if (i > 0) sb.append(',')
            sb.append('"').append(k).append("\":{\"baseUrl\":\"")
                .append(v.baseUrl.replace("\"", "\\\""))
                .append("\",\"name\":\"")
                .append(v.name.replace("\"", "\\\""))
                .append("\"}")
        }
        sb.append('}')
        return sb.toString()
    }

    // Per-sceneId notification ids; base + hash so different
    // scenes don't collide.
    private const val NOTIF_ID_READY_BASE = 20_000
}
