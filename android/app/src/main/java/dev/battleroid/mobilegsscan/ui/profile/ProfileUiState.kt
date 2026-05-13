package dev.battleroid.mobilegsscan.ui.profile

import androidx.compose.runtime.Immutable

/**
 * Snapshot the [ProfileScreen] composable renders from. Driven by
 * [dev.battleroid.mobilegsscan.ProfileActivity], which seeds the
 * device fields from the running runtime (Build.MODEL, BuildConfig
 * version, ServerConfig studio URL).
 *
 * Auth-bearing fields ([displayName], [email]) are placeholder
 * literals until real auth lands — the original Phase 1 plan
 * deliberately scoped auth out.
 */
@Immutable
data class ProfileUiState(
    /** Initials for the avatar circle. Derived from [displayName]
     *  when real auth lands; hardcoded "MW" placeholder for now per
     *  the design source. */
    val initials: String,
    val displayName: String,
    val email: String,
    /** True when a studio URL is configured. Drives the "paired" /
     *  "not paired" chip below the avatar. */
    val paired: Boolean,
    /** Pretty-printed studio host (no scheme). Empty when not
     *  paired. */
    val studioHost: String,
    val deviceModel: String,
    val androidVersion: String,
    val appVersion: String,
)
