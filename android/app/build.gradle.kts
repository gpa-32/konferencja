plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Numer wersji z CI (GITHUB_RUN_NUMBER), lokalnie 1.
val buildNumber = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "pl.epartners.sala"
    compileSdk = 34

    defaultConfig {
        applicationId = "pl.epartners.sala"
        minSdk = 24
        targetSdk = 34
        versionCode = buildNumber
        versionName = "1.0.$buildNumber"
        // Domyślny adres serwera wpisany w ekranie rejestracji
        buildConfigField("String", "DEFAULT_SERVER", "\"https://konferencja.nettrade.pl\"")
    }

    signingConfigs {
        create("release") {
            // Stały klucz – wymagany, żeby aktualizacje OTA instalowały się na istniejącą aplikację.
            val ks = System.getenv("SALA_KEYSTORE") ?: "${rootDir}/keystore/sala.jks"
            storeFile = file(ks)
            storePassword = System.getenv("SALA_KEYSTORE_PASSWORD") ?: "sala-epartners"
            keyAlias = System.getenv("SALA_KEY_ALIAS") ?: "sala"
            keyPassword = System.getenv("SALA_KEY_PASSWORD") ?: "sala-epartners"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
        debug {
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}
