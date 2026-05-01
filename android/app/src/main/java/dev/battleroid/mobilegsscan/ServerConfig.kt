package dev.battleroid.mobilegsscan

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

/**
 * Persists the studio URL ("https://studio.local" or
 * "https://192.168.1.42") between launches. Single-user, single-server
 * — there's no list of saved servers, just the most recent one.
 */
object ServerConfig {
    private const val PREFS = "studio"
    private const val KEY_URL = "studio_url"

    fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun studioUrl(ctx: Context): String? =
        prefs(ctx).getString(KEY_URL, null)?.takeIf { it.isNotBlank() }

    fun setStudioUrl(ctx: Context, url: String) {
        prefs(ctx).edit { putString(KEY_URL, url.trimEnd('/')) }
    }
}
