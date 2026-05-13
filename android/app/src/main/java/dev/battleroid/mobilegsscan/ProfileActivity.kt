package dev.battleroid.mobilegsscan

import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import dev.battleroid.mobilegsscan.ui.profile.ProfileScreen
import dev.battleroid.mobilegsscan.ui.profile.ProfileUiState
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Profile screen — Pebble's account / device summary surface.
 *
 * Auth fields ([ProfileUiState.displayName], [ProfileUiState.email])
 * are placeholder literals because real auth is deferred (per the
 * original Phase 1 plan). Device + pairing fields come from the
 * runtime so what's shown is real.
 *
 * Reachable from [ServerConfigActivity]'s header — there's no slot
 * for a profile button on the home header in the design source.
 * "Sign out" routes to [SignInActivity]; "Unpair this device"
 * clears the studio URL pref and routes to SignIn.
 *
 * Pairing/host state is held in a StateFlow rebuilt in onResume so
 * a round-trip into Settings (which can change the studio URL)
 * reflects on return — without that the paired chip + host
 * subtitle would show stale values until the activity is
 * recreated.
 */
class ProfileActivity : ComponentActivity() {
    private val state: MutableStateFlow<ProfileUiState> by lazy {
        MutableStateFlow(buildState())
    }
    private val uiState: StateFlow<ProfileUiState> by lazy { state.asStateFlow() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                ProfileScreen(
                    state = current,
                    onBackClick = { finish() },
                    onSettingsClick = ::openSettings,
                    onSignOutClick = ::onSignOut,
                    onUnpairClick = ::onUnpair,
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        // Studio URL may have been edited from Settings in the
        // round-trip; rebuild to refresh the paired chip + host
        // subtitle. Cheap — just reads prefs + a few Build fields.
        state.value = buildState()
    }

    private fun buildState(): ProfileUiState {
        val studioUrl = ServerConfig.studioUrl(this)
        val host = studioUrl
            ?.removePrefix("https://")
            ?.removePrefix("http://")
            ?.removeSuffix("/")
            .orEmpty()
        return ProfileUiState(
            // Hardcoded "MW" / "Mira Weston" placeholder mirrors the
            // design source. Replaced with real user data when auth
            // ships.
            initials = "MW",
            displayName = "Mira Weston",
            email = "mira@studio.local",
            paired = !studioUrl.isNullOrBlank(),
            studioHost = host,
            deviceModel = "${Build.MANUFACTURER.replaceFirstChar { it.titlecase() }} ${Build.MODEL}",
            androidVersion = "Android ${Build.VERSION.RELEASE}",
            appVersion = BuildConfig.VERSION_NAME,
        )
    }

    private fun openSettings() {
        // REORDER_TO_FRONT brings the existing Settings activity to
        // the top of the task if one's already underneath (the
        // common case — Profile is currently only reachable from
        // the Settings header). Without this flag every tap pushes
        // a fresh Settings on top of Profile, growing the back
        // stack and making Back behave weirdly (Profile reappears,
        // then a second Settings, etc.). If Settings isn't in the
        // stack (a future direct-entry flow), this just behaves
        // like a normal start.
        startActivity(
            Intent(this, ServerConfigActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
            },
        )
        // Close Profile too — the user wanted to be on Settings,
        // not "Settings stacked on Profile". On return from
        // Settings the user lands on Home directly, which matches
        // the shape of every other Settings exit (Save / Back both
        // finish() and drop to Home).
        finish()
    }

    private fun onSignOut() {
        // Placeholder: there's nothing to clear server-side yet.
        // Route to the SignIn screen so the gesture has somewhere
        // to land.
        startActivity(Intent(this, SignInActivity::class.java))
        finish()
    }

    private fun onUnpair() {
        // "Unpair" is a stronger gesture than sign-out — clear the
        // configured studio URL too so the home screen drops to its
        // not-configured empty state on return. Placeholder copy
        // until the server side has a real device-unregister hook.
        ServerConfig.prefs(this).edit().remove("studio_url").apply()
        startActivity(Intent(this, SignInActivity::class.java))
        finish()
    }
}
