plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 【2026-11-14】版本号从 CI 注入（tag v0.3.0 → 0.3.0），本地不传就用默认值。
// 之前 versionCode 一直是 1：发第二个包时手机可能把新包当旧包，覆盖安装会失败；
// 用户也没法从「设置 → 应用信息」分清装的是哪一版。
// versionCode 用 major*10000 + minor*100 + patch，单调递增。
val versionFromCi = (System.getenv("APP_VERSION") ?: "").trim().removePrefix("v")
val appVersionName = if (versionFromCi.isEmpty()) "0.3.0" else versionFromCi
val versionParts = appVersionName.split(".").map { part ->
    part.filter { ch -> ch in '0'..'9' }.toIntOrNull()
}
val major = versionParts.getOrNull(0)
val minor = versionParts.getOrNull(1)
val patch = versionParts.getOrNull(2)
// 注意：非 tag 构建传进来的是 0.0.0-dev，算出来是 0 —— 而 versionCode 必须 > 0，
// 否则 Gradle 直接报错。所以这里必须兜一个下限 1。
val appVersionCode = if (major != null && minor != null && patch != null) {
    (major * 10000 + minor * 100 + patch).coerceAtLeast(1)
} else {
    1
}

android {
    namespace = "com.jobpilot.mobile"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jobpilot.mobile"
        minSdk = 26
        targetSdk = 34
        versionCode = appVersionCode
        versionName = appVersionName
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

// 刻意零依赖：不引 androidx、不引 Compose。
// 理由：v0.1 只需要一个无障碍服务 + 一个调试页面，
// 每多一个依赖就多一个 CI 首次构建失败的可能。等 D2 真正画界面时再引入。
