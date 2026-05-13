package dev.battleroid.mobilegsscan

import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import dev.battleroid.mobilegsscan.ui.profile.ProfileScreen
import dev.battleroid.mobilegsscan.ui.profile.ProfileUiState
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme

/**
 * Profile screen — Pebble's account / device summary surface.
 *
 * Auth fields ([displayName], [email]) are placeholder literals
 * because real auth is deferred (per the original Phase 1 plan).
 * Device fields ([deviceModel], [appVersion], etc.) come from the
 * runtime so what's shown is real.
 *
 * Reachable from [ServerConfigActivity]'s header — there's no slot
 * for a profile button on the home header in the design source.
 * "Sign out" routes to [SignInActivity]; "Unpair this device"
 * clears the studio URL pref and routes to SignIn.
 */
class ProfileActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        setContent {
            PebbleTheme {
                ProfileScreen(
                    state = buildState(),
                    onBackClick = { finish() },
                    onSettingsClick = ::openSettings,
                    onSignOutClick = ::onSignOut,
                    onUnpairClick = ::onUnpair,
                )
            }
        }
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
        startActivity(Intent(this, ServerConfigActivity::class.java))
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
