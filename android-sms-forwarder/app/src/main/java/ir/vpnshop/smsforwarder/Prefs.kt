package ir.vpnshop.smsforwarder

import android.content.Context

/** Webhook address and on/off switch, kept in plain SharedPreferences. */
object Prefs {
    private const val FILE = "vpnshop_sms_forwarder"
    private const val KEY_URL = "webhook_url"
    private const val KEY_ENABLED = "enabled"
    private const val KEY_LAST = "last_result"

    private fun prefs(context: Context) =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun webhookUrl(context: Context): String =
        prefs(context).getString(KEY_URL, "").orEmpty().trim()

    fun setWebhookUrl(context: Context, value: String) {
        prefs(context).edit().putString(KEY_URL, value.trim()).apply()
    }

    fun isEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_ENABLED, true)

    fun setEnabled(context: Context, value: Boolean) {
        prefs(context).edit().putBoolean(KEY_ENABLED, value).apply()
    }

    fun lastResult(context: Context): String =
        prefs(context).getString(KEY_LAST, "").orEmpty()

    fun setLastResult(context: Context, value: String) {
        prefs(context).edit().putString(KEY_LAST, value).apply()
    }
}
