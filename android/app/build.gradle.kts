plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jobpilot.mobile"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jobpilot.mobile"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
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
