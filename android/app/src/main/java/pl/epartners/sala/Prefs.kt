package pl.epartners.sala

import android.content.Context

class Prefs(ctx: Context) {
    private val sp = ctx.getSharedPreferences("sala", Context.MODE_PRIVATE)

    var server: String
        get() = sp.getString("server", "") ?: ""
        set(v) = sp.edit().putString("server", v.trimEnd('/')).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v).apply()

    var deviceId: Int
        get() = sp.getInt("device_id", 0)
        set(v) = sp.edit().putInt("device_id", v).apply()

    /** Ostatni znany stan sali (JSON) – pokazywany od razu po starcie, zanim odpowie serwer. */
    var lastState: String
        get() = sp.getString("last_state", "") ?: ""
        set(v) = sp.edit().putString("last_state", v).apply()

    val enrolled: Boolean get() = server.isNotEmpty() && token.isNotEmpty()

    fun clear() = sp.edit().clear().apply()
}
