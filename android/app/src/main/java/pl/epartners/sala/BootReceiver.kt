package pl.epartners.sala

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Start aplikacji po włączeniu tabletu i po aktualizacji. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        context.startActivity(
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}
