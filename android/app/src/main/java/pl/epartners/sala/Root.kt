package pl.epartners.sala

import java.io.File
import java.util.concurrent.TimeUnit

/** Polecenia z uprawnieniami root (tablet zrootowany). Bez roota – zwraca false. */
object Root {
    @Volatile private var cached: Boolean? = null

    val available: Boolean
        get() = cached ?: detect().also { cached = it }

    private fun detect(): Boolean {
        val paths = listOf("/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su", "/debug_ramdisk/su")
        if (paths.none { File(it).exists() }) return false
        return run("id").let { it.first && it.second.contains("uid=0") }
    }

    /** Zwraca (sukces, stdout). */
    fun run(cmd: String, timeoutSec: Long = 30): Pair<Boolean, String> = try {
        val p = ProcessBuilder("su", "-c", cmd).redirectErrorStream(true).start()
        val out = p.inputStream.bufferedReader().readText()
        val done = p.waitFor(timeoutSec, TimeUnit.SECONDS)
        if (!done) p.destroy()
        Pair(done && p.exitValue() == 0, out)
    } catch (e: Exception) {
        Pair(false, e.message ?: "")
    }

    fun runBytes(cmd: String): ByteArray? = try {
        val p = ProcessBuilder("su", "-c", cmd).start()
        val bytes = p.inputStream.readBytes()
        p.waitFor(30, TimeUnit.SECONDS)
        if (bytes.isEmpty()) null else bytes
    } catch (e: Exception) {
        null
    }
}
