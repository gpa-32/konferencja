package pl.epartners.sala

import android.app.AlarmManager
import android.app.Application
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import kotlin.system.exitProcess

/**
 * Strażnik aplikacji: po awarii lub zawieszeniu wątku UI aplikacja uruchamia się ponownie.
 */
class App : Application() {

    override fun onCreate() {
        super.onCreate()
        // Bez przekazania do domyślnego handlera – zamiast okna „aplikacja przestała działać” jest restart.
        Thread.setDefaultUncaughtExceptionHandler { _, e ->
            Log.e("Sala", "Awaria – restart aplikacji", e)
            try {
                scheduleRestart(this, 2000)
            } finally {
                android.os.Process.killProcess(android.os.Process.myPid())
                exitProcess(10)
            }
        }
        startMainThreadWatchdog()
    }

    /** Wątek kontrolny: jeśli wątek UI nie odpowiada przez 20 s – restart. */
    private fun startMainThreadWatchdog() {
        val main = Handler(Looper.getMainLooper())
        Thread({
            var tick = 0L
            while (true) {
                val expected = tick + 1
                main.post { tick = expected }
                SystemClock.sleep(20_000)
                if (tick != expected) {
                    Log.e("Sala", "Wątek UI zawieszony – restart aplikacji")
                    restart(this)
                }
            }
        }, "sala-watchdog").apply { isDaemon = true }.start()
    }

    companion object {
        fun scheduleRestart(ctx: Context, delayMs: Long) {
            val intent = Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
            val pi = PendingIntent.getActivity(ctx, 1, intent,
                PendingIntent.FLAG_CANCEL_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
            am.set(AlarmManager.ELAPSED_REALTIME, SystemClock.elapsedRealtime() + delayMs, pi)
        }

        fun restart(ctx: Context) {
            scheduleRestart(ctx, 1500)
            android.os.Process.killProcess(android.os.Process.myPid())
            exitProcess(0)
        }
    }
}
