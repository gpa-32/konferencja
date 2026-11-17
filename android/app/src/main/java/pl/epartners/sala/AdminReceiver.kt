package pl.epartners.sala

import android.app.admin.DeviceAdminReceiver
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent

/**
 * Administrator urządzenia. Jako „device owner” (provisioning QR lub `adb shell dpm set-device-owner
 * pl.epartners.sala/.AdminReceiver`) aplikacja działa w trybie kiosku, instaluje aktualizacje bez pytania
 * i może zrestartować tablet.
 */
class AdminReceiver : DeviceAdminReceiver() {
    override fun onProfileProvisioningComplete(context: Context, intent: Intent) {
        Kiosk.configure(context)
        context.startActivity(Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    companion object {
        fun component(ctx: Context) = ComponentName(ctx, AdminReceiver::class.java)
    }
}

object Kiosk {
    fun dpm(ctx: Context) = ctx.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager

    fun isDeviceOwner(ctx: Context) = dpm(ctx).isDeviceOwnerApp(ctx.packageName)

    /** Ustawienia trybu kiosku dla device ownera: aplikacja na liście lock task i jako ekran główny. */
    fun configure(ctx: Context) {
        if (!isDeviceOwner(ctx)) return
        val dpm = dpm(ctx)
        val admin = AdminReceiver.component(ctx)
        dpm.setLockTaskPackages(admin, arrayOf(ctx.packageName))
        val filter = android.content.IntentFilter(Intent.ACTION_MAIN).apply {
            addCategory(Intent.CATEGORY_HOME)
            addCategory(Intent.CATEGORY_DEFAULT)
        }
        dpm.addPersistentPreferredActivity(admin, filter,
            android.content.ComponentName(ctx, MainActivity::class.java))
        if (android.os.Build.VERSION.SDK_INT >= 28) {
            dpm.setLockTaskFeatures(admin, DevicePolicyManager.LOCK_TASK_FEATURE_NONE)
        }
        dpm.setKeyguardDisabled(admin, true)
        dpm.setStatusBarDisabled(admin, true)
    }

    fun exit(ctx: Context) {
        if (!isDeviceOwner(ctx)) return
        val dpm = dpm(ctx)
        val admin = AdminReceiver.component(ctx)
        dpm.clearPackagePersistentPreferredActivities(admin, ctx.packageName)
        dpm.setStatusBarDisabled(admin, false)
    }

    fun reboot(ctx: Context): Boolean {
        if (Root.available && Root.run("reboot").first) return true
        if (isDeviceOwner(ctx)) {
            dpm(ctx).reboot(AdminReceiver.component(ctx))
            return true
        }
        return false
    }
}
