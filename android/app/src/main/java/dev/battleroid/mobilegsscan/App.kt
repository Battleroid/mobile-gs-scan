package dev.battleroid.mobilegsscan

import android.app.Application
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob

/**
 * App-process singleton — owns the long-lived collaborators that
 * outlive any single activity:
 *
 * * Registers the three notification channels exactly once on
 *   process start (see [NotificationChannels]).
 * * Holds an application-scoped [CoroutineScope] under which
 *   [SceneWatcher] runs its per-scene WebSocket watchers. The
 *   scope survives activity finish / configuration changes — so
 *   when the user finishes an upload and immediately backgrounds
 *   the app, the WS connection keeps running and fires the
 *   "splat ready" notification when the worker pipeline
 *   completes. The scope dies with the process (Android may kill
 *   it under memory pressure), which is the documented limit of
 *   the WS-while-process-is-alive guarantee — FCM is the
 *   follow-up that closes that gap.
 * * On startup, re-attaches WS watchers for any scene ids
 *   persisted by [SceneWatcher.persistedScenes] (e.g. the user
 *   uploaded, backgrounded, the process was killed, they
 *   re-opened the app while the splat is still training).
 */
class App : Application() {
    val appScope: CoroutineScope = CoroutineScope(SupervisorJob())

    override fun onCreate() {
        super.onCreate()
        NotificationChannels.register(this)
        SceneWatcher.restorePending(this)
    }
}
