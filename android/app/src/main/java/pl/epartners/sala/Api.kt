package pl.epartners.sala

import android.os.Build
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

class ApiException(val code: Int, message: String) : Exception(message)

class Api(private val server: String, private val token: String) {

    companion object {
        private val JSON = "application/json".toMediaType()
        val http: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .pingInterval(20, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()

        fun enroll(server: String, body: JSONObject): JSONObject {
            val req = Request.Builder().url("${server.trimEnd('/')}/api/device/enroll")
                .post(body.toString().toRequestBody(JSON)).build()
            return http.newCall(req).execute().use { parse(it) }
        }

        private fun parse(r: Response): JSONObject {
            val text = r.body?.string() ?: ""
            if (!r.isSuccessful) {
                val detail = try {
                    JSONObject(text).optString("detail", text)
                } catch (e: Exception) {
                    text.ifBlank { "HTTP ${r.code}" }
                }
                throw ApiException(r.code, detail.take(300))
            }
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }

    private fun req(path: String) = Request.Builder().url("$server$path").header("Authorization", "Bearer $token")

    fun state(): JSONObject = http.newCall(req("/api/device/state").get().build()).execute().use { parse(it) }

    fun action(body: JSONObject): JSONObject =
        http.newCall(req("/api/device/action").post(body.toString().toRequestBody(JSON)).build()).execute()
            .use { parse(it) }

    fun heartbeat(body: JSONObject) {
        http.newCall(req("/api/device/heartbeat").post(body.toString().toRequestBody(JSON)).build()).execute()
            .use { parse(it) }
    }

    fun uploadScreenshot(png: ByteArray) {
        http.newCall(req("/api/device/screenshot").post(png.toRequestBody("image/png".toMediaType())).build())
            .execute().use { parse(it) }
    }

    fun updateInfo(): JSONObject = http.newCall(req("/api/device/update").get().build()).execute().use { parse(it) }

    fun download(url: String, target: File) {
        val r = Request.Builder().url(url).header("Authorization", "Bearer $token").build()
        http.newCall(r).execute().use { resp ->
            if (!resp.isSuccessful) throw ApiException(resp.code, "Pobieranie aktualizacji: HTTP ${resp.code}")
            target.outputStream().use { out -> resp.body!!.byteStream().copyTo(out) }
        }
    }

    fun webSocket(listener: WebSocketListener): WebSocket {
        val wsUrl = server.replaceFirst("https://", "wss://").replaceFirst("http://", "ws://") +
            "/api/device/ws?token=" + java.net.URLEncoder.encode(token, "UTF-8")
        return http.newWebSocket(Request.Builder().url(wsUrl).build(), listener)
    }
}

fun deviceModel(): String = "${Build.MANUFACTURER} ${Build.MODEL}".trim()
