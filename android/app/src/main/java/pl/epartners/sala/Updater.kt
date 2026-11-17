package pl.epartners.sala

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.util.Log
import java.io.File

/**
 * Aktualizacje OTA z serwera. Kolejność: root (`pm install -r`) → device owner (instalacja cicha
 * przez PackageInstaller) → zwykła instalacja (system zapyta użytkownika).
 */
object Updater {
    private const val TAG = "SalaUpdate"
    @Volatile private var running = false

    fun check(ctx: Context, api: Api, onInfo: (String) -> Unit = {}) {
        if (running) return
        running = true
        Thread {
            try {
                val info = api.updateInfo()
                if (!info.optBoolean("available")) return@Thread
                val code = info.getInt("version_code")
                if (code <= BuildConfig.VERSION_CODE) return@Thread
                onInfo("Pobieranie aktualizacji ${info.optString("version_name")}…")
                val apk = File(ctx.cacheDir, "update.apk")
                api.download(info.getString("url"), apk)
                install(ctx, apk)
            } catch (e: Exception) {
                Log.w(TAG, "Aktualizacja nieudana", e)
                onInfo("Aktualizacja nieudana: ${e.message}")
            } finally {
                running = false
            }
        }.start()
    }

    private fun install(ctx: Context, apk: File) {
        if (Root.available) {
            val tmp = "/data/local/tmp/sala-update.apk"
            val (ok, out) = Root.run("cat '${apk.absolutePath}' > $tmp && pm install -r $tmp; rm -f $tmp", 180)
            Log.i(TAG, "pm install: $ok $out")
            if (ok && out.contains("Success")) return
        }
        val installer = ctx.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(ctx.packageName)
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            session.openWrite("update.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val intent = Intent(ctx, InstallResultReceiver::class.java)
            val pi = PendingIntent.getBroadcast(ctx, id, intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
            session.commit(pi.intentSender)
        }
    }

    class InstallResultReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999)) {
                PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                    @Suppress("DEPRECATION")
                    val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                    confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let { context.startActivity(it) }
                }
                PackageInstaller.STATUS_SUCCESS -> Log.i(TAG, "Zainstalowano aktualizację")
                else -> Log.w(TAG, "Instalacja: ${intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)}")
            }
        }
    }
}
