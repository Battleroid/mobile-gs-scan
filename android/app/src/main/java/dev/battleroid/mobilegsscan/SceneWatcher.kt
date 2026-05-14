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
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
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

    private data class Pending(
        val baseUrl: String,
        val name: String,
        val captureId: String,
    )

    private val watching = ConcurrentHashMap<String, Job>()
    // Serializes the read-modify-write cycle on ``KEY_PENDING``.
    // SharedPreferences itself is thread-safe per-op, but
    // ``persist`` / ``clearPersisted`` each do a load → mutate →
    // write sequence; two scene coroutines firing into that
    // around the same time (one completes and clears while
    // another upload starts and persists) would race the final
    // ``apply()`` and silently drop one of the entries.
    // ``restorePending`` would then miss the dropped scene on
    // the next process start and never deliver its notification.
    private val persistLock = Any()

    fun watch(
        ctx: Context,
        baseUrl: String,
        sceneId: String,
        captureId: String,
        captureName: String,
    ) {
        watchInternal(
            ctx = ctx,
            baseUrl = baseUrl,
            sceneId = sceneId,
            captureId = captureId,
            captureName = captureName,
            persistOnStart = true,
        )
    }

    private fun watchInternal(
        ctx: Context,
        baseUrl: String,
        sceneId: String,
        captureId: String,
        captureName: String,
        persistOnStart: Boolean,
    ) {
        if (sceneId.isBlank() || baseUrl.isBlank()) return
        val app = ctx.applicationContext as App
        if (persistOnStart) {
            // Persist *before* we start the coroutine so a crash
            // mid-connect leaves a recoverable entry behind for
            // restorePending. ``commit()`` rather than ``apply()``
            // — ``apply()`` schedules the disk write asynchronously,
            // so a process kill in the window between ``watch()``
            // returning and the SharedPreferences worker thread
            // flushing would lose the entry entirely; the user
            // would never get a notification for that scene.
            // ``commit()`` is bounded (a few ms for our payload
            // size) and we're only called from the FG service's
            // IO coroutine on this path, so the brief block is
            // fine. Idempotent — repeated watches of the same
            // scene id overwrite the same entry.
            persist(ctx, sceneId, Pending(baseUrl, captureName, captureId))
        }
        if (watching.containsKey(sceneId)) return
        val job = app.appScope.launch {
            try {
                runScene(ctx.applicationContext, baseUrl, sceneId, captureId, captureName)
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
            // ``persistOnStart=false`` — the entry we're
            // re-attaching is exactly what's already on disk;
            // re-writing it would just churn the prefs file
            // (and, on this path, do main-thread I/O from
            // ``App.onCreate``).
            watchInternal(
                ctx = ctx,
                baseUrl = p.baseUrl,
                sceneId = sceneId,
                captureId = p.captureId,
                captureName = p.name,
                persistOnStart = false,
            )
        }
    }

    private suspend fun runScene(
        ctx: Context,
        baseUrl: String,
        sceneId: String,
        captureId: String,
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
                    postReady(ctx, baseUrl, sceneId, captureId, captureName)
                    return
                }
                Terminal.Failed -> {
                    postFailed(ctx, baseUrl, sceneId, captureId, captureName)
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
     *
     * Classifies handshake failures: a 4xx response is treated
     * as terminal (Canceled) so we stop the retry loop —
     * common case is the server's WS endpoint returning 4404
     * "scene not found" after the user / cleanup job deletes
     * the scene. Without this classification ``onFailure``
     * always returned null and ``runScene`` reconnected forever,
     * keeping an app-scoped retry loop alive across process
     * suspensions and racking up wakeups for a scene that will
     * never resolve. 408 / 425 / 429 are retried — they're
     * transient by definition. 5xx and pure network failures
     * fall through to the retry path.
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
                    val code = response?.code
                    response?.close()
                    val terminal: Terminal? = if (code != null && isNonRetryable(code)) {
                        // Stop watching; user will not get a
                        // ready notification for this scene
                        // (it doesn't exist or auth was
                        // rejected). Canceled rather than Failed
                        // because we don't want to fire the
                        // "Processing failed" notification on a
                        // 404 — the scene was already deleted,
                        // the user knows.
                        Terminal.Canceled
                    } else {
                        null
                    }
                    if (cont.isActive) cont.resume(terminal)
                }
            })
            cont.invokeOnCancellation { ws.cancel() }
        }
    }

    private fun isNonRetryable(code: Int): Boolean {
        // 4xx, minus the three retryable codes (request timeout,
        // too early, too many requests).
        if (code !in 400..499) return false
        return code != 408 && code != 425 && code != 429
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
        captureId: String,
        captureName: String,
    ) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        // Tap routes through CaptureDetailActivity rather than
        // straight to the splat viewer: the viewer needs a
        // spz_url which isn't in the WS event payload — only the
        // status flip is. CaptureDetail re-fetches the scene and
        // surfaces the View button with the URL already resolved.
        // ``CaptureDetail.onCreate`` finishes immediately when
        // EXTRA_CAPTURE_ID is empty (line 59 of that file), so
        // we must include the capture id we held onto from the
        // upload result — passing only scene_id would no-op the
        // notification tap and silently drop the user back to
        // the launcher.
        val detail = Intent(ctx, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, captureId)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, captureName)
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
        captureId: String,
        captureName: String,
    ) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        // Failed → route to CaptureDetail so the user can see
        // which job died and retry. CaptureDetail bails on a
        // missing capture id, so include it.
        val detail = Intent(ctx, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, captureId)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, captureName)
            putExtra("scene_id", sceneId)
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
        synchronized(persistLock) {
            val prefs = ctx.applicationContext.getSharedPreferences(
                PREFS_FILE, Context.MODE_PRIVATE,
            )
            val current = loadPersisted(ctx).toMutableMap()
            current[sceneId] = p
            // ``commit()`` rather than ``apply()`` — see watch()
            // for why durability matters here. Cleared writes
            // (clearPersisted) stay on apply(): a clear that
            // doesn't make it to disk just means restorePending
            // re-attaches a watcher that's already terminal,
            // which is benign (the WS reconnects, reads the
            // current snapshot status, fires the right
            // notification, and clears itself again).
            prefs.edit().putString(KEY_PENDING, encode(current)).commit()
        }
    }

    private fun clearPersisted(ctx: Context, sceneId: String) {
        synchronized(persistLock) {
            val prefs = ctx.applicationContext.getSharedPreferences(
                PREFS_FILE, Context.MODE_PRIVATE,
            )
            val current = loadPersisted(ctx).toMutableMap()
            if (current.remove(sceneId) != null) {
                prefs.edit().putString(KEY_PENDING, encode(current)).apply()
            }
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
                    captureId = o["captureId"]?.jsonPrimitive?.content.orEmpty(),
                )
            }
        } catch (_: Exception) {
            emptyMap()
        }
    }

    private fun encode(map: Map<String, Pending>): String {
        // Use the existing ``Json`` instance so we cover the full
        // escape table (backslashes, control chars, embedded
        // quotes, non-BMP code points) — the previous hand-
        // rolled writer only escaped ``"`` and would emit
        // invalid JSON for any capture name with a backslash or
        // newline in it, which then short-circuited
        // ``loadPersisted`` to ``emptyMap()`` on next process
        // start and silently dropped every pending watch.
        val obj = buildJsonObject {
            map.forEach { (k, v) ->
                put(k, buildJsonObject {
                    put("baseUrl", v.baseUrl)
                    put("name", v.name)
                    put("captureId", v.captureId)
                })
            }
        }
        return json.encodeToString(JsonObject.serializer(), obj)
    }

    // Per-sceneId notification ids; base + hash so different
    // scenes don't collide.
    private const val NOTIF_ID_READY_BASE = 20_000
}
