package ir.vpnshop.smsforwarder

import android.content.Context
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/** Posts one SMS to the shop's webhook, retrying while the phone is offline. */
class ForwardWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val url = Prefs.webhookUrl(applicationContext)
        if (url.isEmpty()) return Result.failure()

        val sender = inputData.getString(KEY_SENDER).orEmpty()
        val text = inputData.getString(KEY_TEXT).orEmpty()

        return try {
            val code = post(url, sender, text)
            if (code in 200..299) {
                Prefs.setLastResult(applicationContext, "ارسال شد ($code)")
                Result.success()
            } else if (code in 500..599) {
                // The shop is up but struggling, so try again shortly.
                Prefs.setLastResult(applicationContext, "خطای سرور ($code) — تلاش دوباره")
                Result.retry()
            } else {
                // A 4xx means the address or the secret is wrong; retrying
                // forever would never fix that.
                Prefs.setLastResult(applicationContext, "رد شد ($code) — آدرس وبهوک را بررسی کنید")
                Result.failure()
            }
        } catch (e: Exception) {
            Prefs.setLastResult(applicationContext, "ارتباط برقرار نشد — تلاش دوباره")
            if (runAttemptCount < MAX_ATTEMPTS) Result.retry() else Result.failure()
        }
    }

    private fun post(url: String, sender: String, text: String): Int {
        val body = JSONObject()
            .put("from", sender)
            .put("text", text)
            .toString()
            .toByteArray(Charsets.UTF_8)

        val connection = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 20_000
            readTimeout = 20_000
            doOutput = true
            setRequestProperty("Content-Type", "application/json; charset=utf-8")
        }
        return try {
            connection.outputStream.use { it.write(body) }
            val code = connection.responseCode
            // Draining the stream lets the connection be reused and closed cleanly.
            runCatching {
                (if (code in 200..299) connection.inputStream else connection.errorStream)
                    ?.bufferedReader()?.use(BufferedReader::readText)
            }
            code
        } finally {
            connection.disconnect()
        }
    }

    companion object {
        private const val KEY_SENDER = "sender"
        private const val KEY_TEXT = "text"
        private const val MAX_ATTEMPTS = 5

        fun enqueue(context: Context, sender: String, text: String) {
            val request = OneTimeWorkRequestBuilder<ForwardWorker>()
                .setInputData(
                    Data.Builder()
                        .putString(KEY_SENDER, sender)
                        .putString(KEY_TEXT, text)
                        .build()
                )
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .build()

            // A unique name per message keeps concurrent arrivals from replacing
            // one another.
            WorkManager.getInstance(context).enqueueUniqueWork(
                "sms-${UUID.randomUUID()}",
                ExistingWorkPolicy.KEEP,
                request,
            )
        }
    }
}
