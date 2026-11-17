package pl.epartners.sala

import android.content.Intent
import android.nfc.NfcAdapter
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import org.json.JSONObject

/** Rejestracja tabletu kodem z panelu (QR lub ręcznie). */
class EnrollActivity : AppCompatActivity() {

    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var message: TextView
    private lateinit var btnEnroll: Button

    private val scan = registerForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@registerForActivityResult
        try {
            val j = JSONObject(text)
            server.setText(j.getString("server"))
            code.setText(j.getString("code"))
            enroll()
        } catch (e: Exception) {
            message.text = "To nie jest kod QR z panelu systemu sal."
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_enroll)
        server = findViewById(R.id.server)
        code = findViewById(R.id.code)
        message = findViewById(R.id.message)
        btnEnroll = findViewById(R.id.btnEnroll)
        val prefs = Prefs(this)
        server.setText(prefs.server.ifEmpty { BuildConfig.DEFAULT_SERVER })

        findViewById<Button>(R.id.btnScan).setOnClickListener {
            scan.launch(ScanOptions().setPrompt("Zeskanuj kod QR z panelu").setBeepEnabled(false)
                .setOrientationLocked(false).setDesiredBarcodeFormats(ScanOptions.QR_CODE))
        }
        btnEnroll.setOnClickListener { enroll() }
    }

    private fun enroll() {
        var srv = server.text.toString().trim().trimEnd('/')
        val c = code.text.toString().trim().uppercase()
        if (srv.isEmpty() || c.isEmpty()) {
            message.text = "Podaj adres serwera i kod."
            return
        }
        if (!srv.startsWith("http")) srv = "https://$srv"
        btnEnroll.isEnabled = false
        message.text = "Rejestracja…"
        val body = JSONObject()
            .put("code", c)
            .put("model", deviceModel())
            .put("android_version", Build.VERSION.RELEASE)
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("has_nfc", NfcAdapter.getDefaultAdapter(this) != null)
            .put("has_led", false)
            .put("is_root", Root.available)
            .put("is_device_owner", Kiosk.isDeviceOwner(this))
        Thread {
            try {
                val r = Api.enroll(srv, body)
                val prefs = Prefs(this)
                prefs.server = srv
                prefs.token = r.getString("token")
                prefs.deviceId = r.getInt("device_id")
                runOnUiThread {
                    startActivity(Intent(this, MainActivity::class.java)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
                    finish()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    message.text = "Błąd: ${e.message}"
                    btnEnroll.isEnabled = true
                }
            }
        }.start()
    }
}
