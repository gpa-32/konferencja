package pl.epartners.sala

import org.json.JSONObject

/**
 * Listwa LED wokół tabletu (modele do sal konferencyjnych).
 * Ścieżki sysfs ustawiane w panelu WWW per tablet, np.:
 *   {"red": "/sys/class/leds/red/brightness", "green": "/sys/class/leds/green/brightness", "max": 255}
 * Zapis wymaga roota. Bez konfiguracji – nic nie robi.
 */
object Led {
    enum class Color { GREEN, RED, YELLOW, OFF }

    @Volatile private var last: Color? = null
    @Volatile private var config: JSONObject? = null

    fun configure(cfg: JSONObject?) {
        if (cfg?.toString() != config?.toString()) {
            config = cfg
            last = null
        }
    }

    val supported: Boolean get() = config != null && Root.available

    fun set(color: Color) {
        val cfg = config ?: return
        if (color == last || !Root.available) return
        last = color
        val max = cfg.optInt("max", 255)
        val red = if (color == Color.RED || color == Color.YELLOW) max else 0
        val green = if (color == Color.GREEN || color == Color.YELLOW) max else 0
        val cmds = mutableListOf<String>()
        cfg.optString("red").takeIf { it.isNotBlank() }?.let { cmds += "echo $red > $it" }
        cfg.optString("green").takeIf { it.isNotBlank() }?.let { cmds += "echo $green > $it" }
        cfg.optString("blue").takeIf { it.isNotBlank() }?.let { cmds += "echo 0 > $it" }
        if (cmds.isNotEmpty()) Thread { Root.run(cmds.joinToString("; ")) }.start()
    }

    fun test() {
        Thread {
            for (c in listOf(Color.RED, Color.GREEN, Color.YELLOW, Color.OFF)) {
                last = null
                set(c)
                Thread.sleep(1500)
            }
            last = null
        }.start()
    }
}
