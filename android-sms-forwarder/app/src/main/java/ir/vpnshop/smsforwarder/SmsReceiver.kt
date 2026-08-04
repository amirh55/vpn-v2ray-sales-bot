package ir.vpnshop.smsforwarder

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

/**
 * Picks up incoming SMS and hands them to [ForwardWorker].
 *
 * The work is queued rather than sent here: a receiver gets only a few seconds
 * and its process can be killed straight after, which would lose the message.
 */
class SmsReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        if (!Prefs.isEnabled(context)) return
        if (Prefs.webhookUrl(context).isEmpty()) return

        val messages = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        if (messages.isEmpty()) return

        // A long SMS arrives split into parts; join them back into one message.
        val sender = messages.first().originatingAddress.orEmpty()
        val body = messages.joinToString("") { it.messageBody.orEmpty() }
        if (body.isBlank()) return

        ForwardWorker.enqueue(context, sender, body)
    }
}
