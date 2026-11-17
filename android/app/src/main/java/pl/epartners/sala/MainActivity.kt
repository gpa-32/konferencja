package pl.epartners.sala

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.net.wifi.WifiManager
import android.nfc.NfcAdapter
import android.nfc.Tag
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.util.TypedValue
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.NetworkInterface
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {

    private lateinit var prefs: Prefs
    private lateinit var api: Api
    private val ui = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()

    private lateinit var root: FrameLayout
    private lateinit var roomName: TextView
    private lateinit var status: TextView
    private lateinit var meetingTitle: TextView
    private lateinit var meetingInfo: TextView
    private lateinit var people: TextView
    private lateinit var clock: TextView
    private lateinit var agenda: LinearLayout
    private lateinit var footer: TextView
    private lateinit var blackout: View
    private lateinit var btnBook: Button
    private lateinit var btnEnd: Button
    private lateinit var btnExtend: Button
    private lateinit var btnCheckin: Button

    private var state: JSONObject? = null
    private var serverOffsetMs = 0L
    private var online = false
    private var ws: WebSocket? = null
    private var wsBackoffMs = 2000L
    private var wakeUntil = 0L
    private var nfcAdapter: NfcAdapter? = null
    private var nfcCallback: ((String) -> Unit)? = null
    private var destroyed = false

    // ------------------------------------------------------------------ cykl życia

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        if (!prefs.enrolled) {
            startActivity(Intent(this, EnrollActivity::class.java))
            finish()
            return
        }
        api = Api(prefs.server, prefs.token)
        setContentView(R.layout.activity_main)
        bindViews()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        nfcAdapter = NfcAdapter.getDefaultAdapter(this)
        Kiosk.configure(this)

        prefs.lastState.takeIf { it.isNotBlank() }?.let {
            try {
                render(JSONObject(it))
            } catch (_: Exception) {
            }
        }
        connectWs()
        ui.post(tickClock)
        ui.post(pollState)
        ui.postDelayed(heartbeat, 3000)
        ui.postDelayed(updateCheck, 60_000)
    }

    override fun onResume() {
        super.onResume()
        hideSystemUi()
        if (Kiosk.isDeviceOwner(this)) {
            try {
                startLockTask()
            } catch (_: Exception) {
            }
        }
        nfcAdapter?.let { enableNfcReader(it) }
    }

    override fun onPause() {
        super.onPause()
        try {
            nfcAdapter?.disableReaderMode(this)
        } catch (_: Exception) {
        }
    }

    override fun onDestroy() {
        destroyed = true
        ui.removeCallbacksAndMessages(null)
        ws?.close(1000, "bye")
        io.shutdownNow()
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java")
    @SuppressLint("MissingSuperCall")
    override fun onBackPressed() {
        // Kiosk: przycisk Wstecz nic nie robi.
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUi()
    }

    private fun bindViews() {
        root = findViewById(R.id.root)
        roomName = findViewById(R.id.roomName)
        status = findViewById(R.id.status)
        meetingTitle = findViewById(R.id.meetingTitle)
        meetingInfo = findViewById(R.id.meetingInfo)
        people = findViewById(R.id.people)
        clock = findViewById(R.id.clock)
        agenda = findViewById(R.id.agenda)
        footer = findViewById(R.id.footer)
        blackout = findViewById(R.id.blackout)
        btnBook = findViewById(R.id.btnBook)
        btnEnd = findViewById(R.id.btnEnd)
        btnExtend = findViewById(R.id.btnExtend)
        btnCheckin = findViewById(R.id.btnCheckin)

        btnBook.setOnClickListener { chooseBookTime() }
        btnExtend.setOnClickListener { chooseExtendTime() }
        btnEnd.setOnClickListener {
            val id = currentId() ?: return@setOnClickListener
            confirm("Zakończyć spotkanie i zwolnić salę?") { doAction("end", id) }
        }
        btnCheckin.setOnClickListener { currentId()?.let { doAction("checkin", it) } }
        blackout.setOnClickListener { wakeUntil = System.currentTimeMillis() + 60_000; applyScreenSchedule() }

        var taps = 0
        var firstTap = 0L
        findViewById<View>(R.id.serviceCorner).setOnClickListener {
            val now = System.currentTimeMillis()
            if (now - firstTap > 3000) {
                taps = 0
                firstTap = now
            }
            if (++taps >= 5) {
                taps = 0
                showServiceInfo()
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun hideSystemUi() {
        if (Build.VERSION.SDK_INT >= 30) {
            window.insetsController?.let {
                it.hide(WindowInsets.Type.statusBars() or WindowInsets.Type.navigationBars())
                it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or
                View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
        }
    }

    // ------------------------------------------------------------------ pętle

    private val tickClock = object : Runnable {
        override fun run() {
            val tz = state?.optString("timezone")?.takeIf { it.isNotBlank() }
            val fmt = SimpleDateFormat("HH:mm", Locale("pl", "PL"))
            if (tz != null) fmt.timeZone = TimeZone.getTimeZone(tz)
            clock.text = fmt.format(Date(nowServer()))
            updateCheckinCountdown()
            applyScreenSchedule()
            ui.postDelayed(this, 1000)
        }
    }

    private val pollState = object : Runnable {
        override fun run() {
            refresh()
            // Gdy WebSocket działa – rzadziej (zmiany przychodzą na żywo).
            ui.postDelayed(this, if (online && ws != null) 60_000 else 20_000)
        }
    }

    private val heartbeat = object : Runnable {
        override fun run() {
            io.execute {
                try {
                    api.heartbeat(JSONObject()
                        .put("battery", batteryLevel())
                        .put("ip", localIp())
                        .put("app_version", BuildConfig.VERSION_NAME))
                } catch (_: Exception) {
                }
            }
            ui.postDelayed(this, 60_000)
        }
    }

    private val updateCheck = object : Runnable {
        override fun run() {
            Updater.check(this@MainActivity, api) { msg -> ui.post { footerMsg(msg) } }
            ui.postDelayed(this, 6 * 3600_000L)
        }
    }

    private fun nowServer() = System.currentTimeMillis() + serverOffsetMs

    // ------------------------------------------------------------------ serwer

    private fun refresh() {
        io.execute {
            try {
                val s = api.state()
                prefs.lastState = s.toString()
                ui.post { online = true; render(s) }
            } catch (e: ApiException) {
                ui.post {
                    online = false
                    if (e.code == 401) unenrolled() else footerMsg("Błąd serwera: ${e.message}")
                }
            } catch (e: Exception) {
                ui.post { online = false; footerMsg("Brak połączenia z serwerem") }
            }
        }
    }

    private fun doAction(action: String, bookingId: Int? = null, minutes: Int? = null) {
        val needAuth = state?.optJSONObject("device")?.optBoolean("require_auth") == true
        if (needAuth) {
            askAuth { pin, nfc -> sendAction(action, bookingId, minutes, pin, nfc) }
        } else {
            sendAction(action, bookingId, minutes, null, null)
        }
    }

    private fun sendAction(action: String, bookingId: Int?, minutes: Int?, pin: String?, nfc: String?) {
        val body = JSONObject().put("action", action)
        bookingId?.let { body.put("booking_id", it) }
        minutes?.let { body.put("minutes", it) }
        pin?.let { body.put("pin", it) }
        nfc?.let { body.put("nfc_uid", it) }
        setButtonsEnabled(false)
        io.execute {
            try {
                val s = api.action(body)
                prefs.lastState = s.toString()
                ui.post { setButtonsEnabled(true); render(s) }
            } catch (e: Exception) {
                ui.post {
                    setButtonsEnabled(true)
                    toast(e.message ?: "Błąd")
                    refresh()
                }
            }
        }
    }

    private fun unenrolled() {
        prefs.clear()
        startActivity(Intent(this, EnrollActivity::class.java))
        finish()
    }

    // ------------------------------------------------------------------ WebSocket

    private fun connectWs() {
        if (destroyed) return
        ws = api.webSocket(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                wsBackoffMs = 2000
                ui.post { online = true; refresh() }
                ui.post(wsPing)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val msg = try {
                    JSONObject(text)
                } catch (_: Exception) {
                    return
                }
                ui.post {
                    when (msg.optString("type")) {
                        "refresh" -> refresh()
                        "command" -> handleCommand(msg.optString("command"))
                    }
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = reconnect(webSocket, code)

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                reconnect(webSocket, response?.code ?: 0)
        })
    }

    private fun reconnect(socket: WebSocket, code: Int) {
        ui.post {
            if (ws !== socket || destroyed) return@post
            ws = null
            ui.removeCallbacks(wsPing)
            if (code == 4401) {
                refresh() // 401 z /state wyrejestruje tablet
            }
            ui.postDelayed({ connectWs() }, wsBackoffMs)
            wsBackoffMs = (wsBackoffMs * 2).coerceAtMost(60_000)
        }
    }

    private val wsPing = object : Runnable {
        override fun run() {
            ws?.send("ping")
            if (ws != null) ui.postDelayed(this, 30_000)
        }
    }

    private fun handleCommand(cmd: String) {
        when (cmd) {
            "reload" -> refresh()
            "restart_app" -> App.restart(this)
            "reboot" -> io.execute { if (!Kiosk.reboot(this)) ui.post { toast("Restart wymaga roota lub trybu kiosku") } }
            "screenshot" -> takeScreenshot()
            "update" -> Updater.check(this, api) { msg -> ui.post { footerMsg(msg) } }
            "led_test" -> Led.test()
            "exit_kiosk" -> {
                Kiosk.exit(this)
                try {
                    stopLockTask()
                } catch (_: Exception) {
                }
                startActivity(Intent(Settings.ACTION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
            "unenroll" -> unenrolled()
        }
    }

    private fun takeScreenshot() {
        val viewShot: ByteArray? = if (!Root.available) {
            val v = window.decorView
            val bmp = Bitmap.createBitmap(v.width.coerceAtLeast(1), v.height.coerceAtLeast(1), Bitmap.Config.ARGB_8888)
            v.draw(Canvas(bmp))
            val out = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.PNG, 90, out)
            bmp.recycle()
            out.toByteArray()
        } else null
        io.execute {
            try {
                val png = viewShot ?: Root.runBytes("screencap -p") ?: return@execute
                api.uploadScreenshot(png)
            } catch (_: Exception) {
            }
        }
    }

    // ------------------------------------------------------------------ widok

    private fun render(s: JSONObject) {
        state = s
        serverOffsetMs = s.optLong("server_time", System.currentTimeMillis()) - System.currentTimeMillis()
        val dev = s.optJSONObject("device")
        Led.configure(dev?.optJSONObject("led_config"))
        dev?.let { applyBrightness(it.optInt("brightness", 80)) }

        val room = s.optJSONObject("room")
        if (room == null) {
            setBg(R.color.off)
            roomName.text = dev?.optString("name") ?: "Tablet"
            status.text = "Nieprzypisany"
            meetingTitle.text = "Przypisz tablet do sali w panelu WWW."
            meetingInfo.text = ""
            people.text = ""
            agenda.removeAllViews()
            listOf(btnBook, btnEnd, btnExtend, btnCheckin).forEach { it.visibility = View.GONE }
            Led.set(Led.Color.OFF)
            footerMsg("")
            return
        }
        roomName.text = room.optString("name")
        val current = s.optJSONObject("current")
        when (s.optString("status")) {
            "free" -> {
                setBg(R.color.free)
                status.text = "WOLNA"
                val until = s.optString("free_until_local").takeIf { it.isNotBlank() && it != "null" }
                meetingTitle.text = if (until != null) "Wolna do $until" else "Wolna do końca dnia"
                meetingInfo.text = ""
                people.text = "Pojemność: ${room.optInt("capacity")} os."
                Led.set(Led.Color.GREEN)
            }
            "pending" -> {
                setBg(R.color.pending)
                status.text = "ZAJĘTA"
                Led.set(Led.Color.YELLOW)
            }
            else -> {
                setBg(R.color.busy)
                status.text = "ZAJĘTA"
                Led.set(Led.Color.RED)
            }
        }
        if (current != null) {
            meetingTitle.text = current.optString("title")
            val org = current.optString("organizer").takeIf { it.isNotBlank() }
            meetingInfo.text = "${current.optString("start_local")} – ${current.optString("end_local")}" +
                (org?.let { "  ·  $it" } ?: "") +
                (if (current.optString("status") == "pending_approval") "\nCzeka na zatwierdzenie opiekuna sali" else "")
            people.text = "${current.optInt("people")} / ${current.optInt("capacity")} os."
        }

        // Przyciski
        val bookOpts = s.optJSONArray("book_options")
        val extOpts = s.optJSONArray("extend_options")
        btnBook.visibility = if (current == null && bookOpts != null && bookOpts.length() > 0) View.VISIBLE else View.GONE
        btnEnd.visibility = if (current != null) View.VISIBLE else View.GONE
        btnExtend.visibility = if (current != null && extOpts != null && extOpts.length() > 0) View.VISIBLE else View.GONE
        val needsCheckin = current?.optBoolean("needs_checkin") == true ||
            s.optJSONArray("next")?.let { arr -> (0 until arr.length()).any { arr.getJSONObject(it).optBoolean("needs_checkin") } } == true
        btnCheckin.visibility = if (needsCheckin) View.VISIBLE else View.GONE

        // Agenda
        agenda.removeAllViews()
        val next = s.optJSONArray("next")
        if (next == null || next.length() == 0) {
            agenda.addView(agendaItem("Brak kolejnych spotkań", "", false))
        } else {
            for (i in 0 until next.length()) {
                val b = next.getJSONObject(i)
                agenda.addView(agendaItem(
                    "${b.optString("start_local")} – ${b.optString("end_local")}",
                    b.optString("title") + (b.optString("organizer").takeIf { it.isNotBlank() }?.let { "\n$it" } ?: ""),
                    b.optString("status") == "pending_approval"))
            }
        }
        footerMsg("")
        updateCheckinCountdown()
    }

    private fun agendaItem(time: String, title: String, pending: Boolean): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(12), 0, dp(12))
        }
        box.addView(TextView(this).apply {
            text = time + if (pending) "  (czeka na zatwierdzenie)" else ""
            setTextColor(ContextCompat.getColor(context, R.color.white))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
        })
        if (title.isNotBlank()) box.addView(TextView(this).apply {
            text = title
            setTextColor(ContextCompat.getColor(context, R.color.white_dim))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
        })
        return box
    }

    private fun updateCheckinCountdown() {
        val cur = state?.optJSONObject("current") ?: return
        if (!cur.optBoolean("needs_checkin")) return
        val deadline = cur.optLong("checkin_deadline", 0)
        if (deadline <= 0) return
        val left = (deadline - nowServer()) / 1000
        btnCheckin.text = if (left > 0) "Potwierdź obecność (${left / 60}:${"%02d".format(left % 60)})" else "Potwierdź obecność"
    }

    private fun setBg(color: Int) = root.setBackgroundColor(ContextCompat.getColor(this, color))

    private fun setButtonsEnabled(on: Boolean) =
        listOf(btnBook, btnEnd, btnExtend, btnCheckin).forEach { it.isEnabled = on; it.alpha = if (on) 1f else 0.5f }

    private fun footerMsg(extra: String) {
        val base = (if (online) "online" else "offline") + " · ${prefs.server.substringAfter("://")} · v${BuildConfig.VERSION_NAME}"
        footer.text = if (extra.isNotBlank()) "$extra\n$base" else base
    }

    private fun applyBrightness(percent: Int) {
        if (blackout.visibility == View.VISIBLE) return
        val lp = window.attributes
        lp.screenBrightness = (percent.coerceIn(5, 100)) / 100f
        window.attributes = lp
    }

    /** Wygaszanie ekranu po godzinach (harmonogram z panelu); dotknięcie wybudza na 60 s. */
    private fun applyScreenSchedule() {
        val dev = state?.optJSONObject("device") ?: return
        val from = dev.optString("screen_off_from").takeIf { it.length == 5 }
        val to = dev.optString("screen_off_to").takeIf { it.length == 5 }
        var off = false
        if (from != null && to != null) {
            val cal = Calendar.getInstance(TimeZone.getTimeZone(state?.optString("timezone") ?: "Europe/Warsaw"))
            cal.timeInMillis = nowServer()
            val nowMin = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
            val f = from.substring(0, 2).toInt() * 60 + from.substring(3).toInt()
            val t = to.substring(0, 2).toInt() * 60 + to.substring(3).toInt()
            off = if (f <= t) nowMin in f until t else (nowMin >= f || nowMin < t)
        }
        if (System.currentTimeMillis() < wakeUntil) off = false
        if (off && blackout.visibility != View.VISIBLE) {
            blackout.visibility = View.VISIBLE
            window.attributes = window.attributes.also { it.screenBrightness = 0.01f }
        } else if (!off && blackout.visibility == View.VISIBLE) {
            blackout.visibility = View.GONE
            applyBrightness(dev.optInt("brightness", 80))
        }
    }

    // ------------------------------------------------------------------ dialogi

    private fun currentId(): Int? = state?.optJSONObject("current")?.optInt("id")?.takeIf { it > 0 }
        ?: state?.optJSONArray("next")?.let { arr ->
            (0 until arr.length()).map { arr.getJSONObject(it) }.firstOrNull { it.optBoolean("needs_checkin") }?.optInt("id")
        }

    private fun chooseBookTime() {
        val opts = state?.optJSONArray("book_options") ?: return
        val minutes = (0 until opts.length()).map { opts.getInt(it) }
        AlertDialog.Builder(this)
            .setTitle("Zarezerwuj salę na")
            .setItems(minutes.map { label(it) }.toTypedArray()) { _, i -> doAction("book_now", minutes = minutes[i]) }
            .setNegativeButton("Anuluj", null)
            .show().also { autoDismiss(it) }
    }

    private fun chooseExtendTime() {
        val id = currentId() ?: return
        val opts = state?.optJSONArray("extend_options") ?: return
        val minutes = (0 until opts.length()).map { opts.getInt(it) }
        AlertDialog.Builder(this)
            .setTitle("Przedłuż spotkanie o")
            .setItems(minutes.map { label(it) }.toTypedArray()) { _, i -> doAction("extend", id, minutes[i]) }
            .setNegativeButton("Anuluj", null)
            .show().also { autoDismiss(it) }
    }

    private fun label(m: Int) = if (m >= 60) "${m / 60} h${if (m % 60 > 0) " ${m % 60} min" else ""}" else "$m min"

    private fun confirm(msg: String, ok: () -> Unit) {
        AlertDialog.Builder(this).setMessage(msg)
            .setPositiveButton("Tak") { _, _ -> ok() }
            .setNegativeButton("Nie", null)
            .show().also { autoDismiss(it) }
    }

    /** PIN albo karta NFC (jeśli tablet ma czytnik). */
    private fun askAuth(onAuth: (String?, String?) -> Unit) {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
            hint = "PIN"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 28f)
        }
        val box = FrameLayout(this).apply { setPadding(dp(24), dp(8), dp(24), 0); addView(input) }
        val dialog = AlertDialog.Builder(this)
            .setTitle(if (nfcAdapter != null) "Podaj PIN lub przyłóż kartę" else "Podaj PIN")
            .setView(box)
            .setPositiveButton("OK") { _, _ -> onAuth(input.text.toString(), null) }
            .setNegativeButton("Anuluj", null)
            .create()
        nfcCallback = { uid ->
            if (dialog.isShowing) {
                dialog.dismiss()
                onAuth(null, uid)
            }
        }
        dialog.setOnDismissListener { nfcCallback = null }
        dialog.show()
        autoDismiss(dialog)
    }

    private fun autoDismiss(d: AlertDialog) = ui.postDelayed({ if (d.isShowing) d.dismiss() }, 45_000)

    private fun showServiceInfo() {
        AlertDialog.Builder(this)
            .setTitle("Informacje serwisowe")
            .setMessage("Serwer: ${prefs.server}\nID tabletu: ${prefs.deviceId}\nWersja: ${BuildConfig.VERSION_NAME}\n" +
                "Model: ${deviceModel()} · Android ${Build.VERSION.RELEASE}\nIP: ${localIp()}\n" +
                "Root: ${if (Root.available) "tak" else "nie"} · Kiosk: ${if (Kiosk.isDeviceOwner(this)) "tak" else "nie"}\n\n" +
                "Wyjście z trybu kiosku i wyrejestrowanie – z panelu WWW.")
            .setPositiveButton("Odśwież") { _, _ -> refresh() }
            .setNegativeButton("Zamknij", null)
            .show().also { autoDismiss(it) }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    // ------------------------------------------------------------------ NFC i system

    private fun enableNfcReader(adapter: NfcAdapter) {
        val flags = NfcAdapter.FLAG_READER_NFC_A or NfcAdapter.FLAG_READER_NFC_B or
            NfcAdapter.FLAG_READER_NFC_F or NfcAdapter.FLAG_READER_NFC_V or NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK
        try {
            adapter.enableReaderMode(this, { tag: Tag ->
                val uid = tag.id.joinToString("") { "%02X".format(it) }
                ui.post {
                    val cb = nfcCallback
                    if (cb != null) cb(uid) else toast("Karta $uid – najpierw wybierz akcję")
                }
            }, flags, null)
        } catch (_: Exception) {
        }
    }

    private fun batteryLevel(): Int? {
        val bm = getSystemService(Context.BATTERY_SERVICE) as? BatteryManager ?: return null
        return bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY).takeIf { it in 0..100 }
    }

    private fun localIp(): String = try {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { it.inetAddresses.toList() }
            .firstOrNull { it is java.net.Inet4Address }?.hostAddress ?: ""
    } catch (_: Exception) {
        @Suppress("DEPRECATION")
        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        @Suppress("DEPRECATION")
        wm?.connectionInfo?.ipAddress?.let { "%d.%d.%d.%d".format(it and 0xff, it shr 8 and 0xff, it shr 16 and 0xff, it shr 24 and 0xff) } ?: ""
    }
}
