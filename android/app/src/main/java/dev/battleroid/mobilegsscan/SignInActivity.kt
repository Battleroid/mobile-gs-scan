package dev.battleroid.mobilegsscan

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import dev.battleroid.mobilegsscan.ui.signin.SignInScreen
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme

/**
 * Sign-in / Pair-to-Studio screen — placeholder.
 *
 * Phase 1 deliberately scoped real auth out; this screen exists so
 * the Profile flow has somewhere to land users on Sign-out /
 * Unpair, and so a future first-launch onboarding has a hook.
 *
 * The screen does NOT actually start a QR scanner — we don't ship
 * a QR-decoding dependency yet. The reticle is a static visual.
 * The only working CTA is "Enter Studio URL instead", which routes
 * to [ServerConfigActivity].
 */
class SignInActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        setContent {
            PebbleTheme {
                SignInScreen(
                    onCloseClick = ::onClose,
                    onEnterUrlClick = ::onEnterUrl,
                )
            }
        }
    }

    private fun onClose() {
        // Route back to home rather than just finishing — on a
        // first-launch flow the user might not have a back stack
        // here. CLEAR_TOP collapses any back-stack into Main if it
        // already exists.
        startActivity(
            Intent(this, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            },
        )
        finish()
    }

    private fun onEnterUrl() {
        // Don't push Settings directly — if the user reached SignIn
        // via Profile → "Sign out", there's a Settings activity
        // underneath in the back stack. Starting another one stacks
        // them; Back from the new Settings returns to the old one
        // rather than Home, which is confusing.
        //
        // Route through MainActivity with CLEAR_TOP so any prior
        // Settings / Profile / SignIn in the back stack collapses
        // to Main, then push a fresh Settings on top. Back from
        // Settings now goes to Home regardless of how SignIn was
        // reached.
        val home = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val settings = Intent(this, ServerConfigActivity::class.java)
        startActivities(arrayOf(home, settings))
        finish()
    }
}
