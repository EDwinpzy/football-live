package com.qiubo.optimaltv.mobileui

import android.graphics.Bitmap
import android.os.ParcelFileDescriptor
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File

internal fun savePlayerUiEvidence(name: String, bitmap: Bitmap) {
    val instrumentation = InstrumentationRegistry.getInstrumentation()
    val directory = File(instrumentation.targetContext.getExternalFilesDir(null), "qa-evidence")
    directory.mkdirs()
    val screenshot = File(directory, "$name.png")
    screenshot.outputStream().use {
        check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it))
    }
    bitmap.recycle()
    // connectedAndroidTest removes the fixture APK afterward; preserve evidence outside its data.
    fun shell(command: String) {
        ParcelFileDescriptor.AutoCloseInputStream(instrumentation.uiAutomation.executeShellCommand(command))
            .use { it.readBytes() }
    }
    shell("mkdir -p /sdcard/Download/mytv-qa-evidence")
    shell("cp ${screenshot.absolutePath} /sdcard/Download/mytv-qa-evidence/$name.png")
}
