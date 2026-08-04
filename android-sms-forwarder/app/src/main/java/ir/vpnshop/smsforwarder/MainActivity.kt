package ir.vpnshop.smsforwarder

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import ir.vpnshop.smsforwarder.databinding.ActivityMainBinding
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    private val requestSms = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (!granted) {
            toast(getString(R.string.permission_needed))
        }
        refreshStatus()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.webhookInput.setText(Prefs.webhookUrl(this))
        binding.enabledSwitch.isChecked = Prefs.isEnabled(this)

        binding.enabledSwitch.setOnCheckedChangeListener { _, checked ->
            Prefs.setEnabled(this, checked)
            refreshStatus()
        }

        binding.saveButton.setOnClickListener { save() }
        binding.testButton.setOnClickListener { sendTest() }
        binding.batteryButton.setOnClickListener { openBatterySettings() }

        ensureSmsPermission()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun save() {
        val url = binding.webhookInput.text.toString().trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            toast(getString(R.string.url_invalid))
            return
        }
        Prefs.setWebhookUrl(this, url)
        toast(getString(R.string.saved))
        refreshStatus()
    }

    /** Sends a sample deposit message so the whole chain can be checked at once. */
    private fun sendTest() {
        val url = binding.webhookInput.text.toString().trim()
        if (!url.startsWith("http")) {
            toast(getString(R.string.url_invalid))
            return
        }
        Prefs.setWebhookUrl(this, url)
        binding.statusText.text = getString(R.string.testing)

        thread {
            val result = runCatching {
                val body = JSONObject()
                    .put("from", "TEST")
                    .put("text", "تست اتصال از اپ فوروارد پیامک")
                    .toString()
                    .toByteArray(Charsets.UTF_8)
                val connection = (URL(url).openConnection() as HttpURLConnection).apply {
                    requestMethod = "POST"
                    connectTimeout = 20_000
                    readTimeout = 20_000
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json; charset=utf-8")
                }
                try {
                    connection.outputStream.use { it.write(body) }
                    connection.responseCode
                } finally {
                    connection.disconnect()
                }
            }

            runOnUiThread {
                val text = result.fold(
                    onSuccess = { code ->
                        if (code in 200..299) getString(R.string.test_ok, code)
                        else getString(R.string.test_rejected, code)
                    },
                    onFailure = { getString(R.string.test_failed) },
                )
                Prefs.setLastResult(this, text)
                refreshStatus()
            }
        }
    }

    private fun ensureSmsPermission() {
        if (!hasSmsPermission()) {
            requestSms.launch(Manifest.permission.RECEIVE_SMS)
        }
    }

    private fun hasSmsPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECEIVE_SMS) ==
            PackageManager.PERMISSION_GRANTED

    private fun isIgnoringBatteryOptimizations(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true
        val power = getSystemService(POWER_SERVICE) as PowerManager
        return power.isIgnoringBatteryOptimizations(packageName)
    }

    private fun openBatterySettings() {
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName"),
            )
        } else {
            Intent(Settings.ACTION_SETTINGS)
        }
        runCatching { startActivity(intent) }
            .onFailure { startActivity(Intent(Settings.ACTION_SETTINGS)) }
    }

    private fun refreshStatus() {
        val lines = mutableListOf<String>()
        lines += if (hasSmsPermission()) getString(R.string.status_sms_ok)
        else getString(R.string.status_sms_missing)

        lines += if (Prefs.webhookUrl(this).isNotEmpty()) getString(R.string.status_url_ok)
        else getString(R.string.status_url_missing)

        lines += if (isIgnoringBatteryOptimizations()) getString(R.string.status_battery_ok)
        else getString(R.string.status_battery_warn)

        lines += if (Prefs.isEnabled(this)) getString(R.string.status_on)
        else getString(R.string.status_off)

        val last = Prefs.lastResult(this)
        if (last.isNotEmpty()) lines += getString(R.string.status_last, last)

        binding.statusText.text = lines.joinToString("\n")
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }
}
