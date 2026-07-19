#include <obs-module.h>
#include <obs-frontend-api.h>

#include <QDockWidget>
#include <QWidget>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QGroupBox>
#include <QTabWidget>
#include <QTextEdit>
#include <QSettings>
#include <QTimer>
#include <QStandardPaths>
#include <QDir>
#include <QFile>
#include <QTextStream>
#include <QFileInfo>
#include <QProcess>
#include <QThread>
#include <QDateTime>
#include <QApplication>
#include <QDesktopServices>
#include <QUrl>
#include <QJsonDocument>
#include <QJsonObject>
#include <QScrollArea>
#include <QFormLayout>
#include <QCheckBox>
#include <QSpinBox>
#include <QColorDialog>
#include <QFileDialog>
#include <QFrame>
#include <QMap>
#include <QTcpSocket>
#include <QRandomGenerator>

#include <filesystem>
#include <string>
#include <algorithm>

#include "browser-panel.hpp"

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("streamdirector-alerts", "en-US")

static QCef *cef = nullptr;
static QDockWidget *alertsDock = nullptr;
static QCefWidget *cefWidget = nullptr;
static QLineEdit *tokenEdit = nullptr;
static QLabel *connStatus = nullptr;
static QTextEdit *alertsLog = nullptr;
static QPushButton *connectBtn = nullptr;
static QPushButton *disconnectBtn = nullptr;
static QPushButton *loginBtn = nullptr;
static bool alertsRunning = false;
static bool loginInProgress = false;
static QProcess *alertsProcess = nullptr;

static QString GetConfigDir()
{
	QString base = qEnvironmentVariable("APPDATA");
	if (base.isEmpty())
		base = QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation);
	QString dir = base + "/streamdirector/streamdirector-alerts";
	QDir().mkpath(dir);
	return dir;
}

static QString GetTokenFilePath()
{
	return GetConfigDir() + "/twitch_tokens.json";
}

static QString LoadAccessToken()
{
	QString path = GetTokenFilePath();
	blog(LOG_INFO, "[streamdirector-alerts] Looking for tokens at: %s", path.toUtf8().constData());
	QFile f(path);
	if (f.open(QIODevice::ReadOnly | QIODevice::Text)) {
		QJsonDocument doc = QJsonDocument::fromJson(f.readAll());
		QString token = doc.object().value("access_token").toString();
		blog(LOG_INFO, "[streamdirector-alerts] Loaded access token: %s", token.isEmpty() ? "(empty)" : "(found)");
		return token;
	}
	blog(LOG_INFO, "[streamdirector-alerts] Could not open token file: %s", path.toUtf8().constData());
	return "";
}

static QString LoadRefreshToken()
{
	QString path = GetTokenFilePath();
	QFile f(path);
	if (f.open(QIODevice::ReadOnly | QIODevice::Text)) {
		QJsonDocument doc = QJsonDocument::fromJson(f.readAll());
		return doc.object().value("refresh_token").toString();
	}
	return "";
}

static void SaveTokens(const QString &accessToken, const QString &refreshToken)
{
	QJsonObject obj;
	obj["access_token"] = accessToken;
	obj["refresh_token"] = refreshToken;
	QJsonDocument doc(obj);
	QFile f(GetTokenFilePath());
	if (f.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
		f.write(doc.toJson());
	}
}

static std::string GetAlertsHtmlPath()
{
	char *dataPath = obs_module_file("alerts.html");
	if (dataPath) {
		std::string path(dataPath);
		bfree(dataPath);
		try {
			auto absPath = std::filesystem::absolute(path);
			return "file:///" + absPath.generic_string();
		} catch (...) {
			std::replace(path.begin(), path.end(), '\\', '/');
			return "file:///" + path;
		}
	}
	return "";
}

static void LogAlert(const QString &msg)
{
	QString timestamp = QDateTime::currentDateTime().toString("hh:mm:ss");
	alertsLog->append(QString("[%1] %2").arg(timestamp, msg));
}

static void StartAlertsBackend()
{
	if (alertsRunning) {
		LogAlert("Alerts backend already running.");
		return;
	}

	/* Kill any previous alerts process */
	if (alertsProcess) {
		alertsProcess->kill();
		alertsProcess->waitForFinished(1000);
		alertsProcess->deleteLater();
		alertsProcess = nullptr;
	}
	alertsRunning = false;

	QString token = tokenEdit->text().trimmed();
	if (token.isEmpty()) {
		LogAlert("ERROR: No Twitch OAuth token set. Click 'Login with Twitch' to authenticate.");
		connStatus->setText("Status: No token");
		connStatus->setStyleSheet("color: #e44;");
		return;
	}

	QString refreshToken = LoadRefreshToken();
	SaveTokens(token, refreshToken);

	/* Call into the Python bridge to start the alerts backend */
	/* We use the streamdirector-agent's PythonBridge since Python is already initialized */
	blog(LOG_INFO, "[streamdirector-alerts] Starting alerts backend...");

	/* Try to call the Python alerts runner */
	typedef bool (*RunAlertsFunc)(const char *token, const char *action);
	/* We'll use obs_module_get_binary_path to find the agent plugin, but actually
	   we can just use the embedded Python directly via a simpler approach:
	   write the token to a known location and have the Python script pick it up */

	/* For now, start the Python alerts script via QProcess */
	QString pythonScript;
	char *scriptPath = obs_module_file("sd_alerts.py");
	if (scriptPath) {
		pythonScript = QString::fromUtf8(scriptPath);
		bfree(scriptPath);
	}

	if (pythonScript.isEmpty()) {
		LogAlert("ERROR: sd_alerts.py not found in plugin data.");
		return;
	}

	/* Find Python executable - use the system Python since the agent plugin
	   already has Python initialized, but we need a separate process for the
	   WebSocket server to avoid GIL issues */
	QString pythonExe = "python";

	/* Get config path - user config dir takes priority */
	QString userConfigPath = GetConfigDir() + "/alerts_config.json";
	QString defaultConfigPath;
	char *dataConfigPath = obs_module_file("alerts_config.json");
	if (dataConfigPath) {
		defaultConfigPath = QString::fromUtf8(dataConfigPath);
		bfree(dataConfigPath);
	}

	/* Copy default config to user dir if it doesn't exist */
	if (!QFile::exists(userConfigPath) && !defaultConfigPath.isEmpty()) {
		QFile::copy(defaultConfigPath, userConfigPath);
	}

	QStringList args;
	args << pythonScript << "--token" << token << "--config" << userConfigPath;
	if (!refreshToken.isEmpty()) {
		args << "--refresh-token" << refreshToken;
	}

	QProcess *proc = new QProcess();
	alertsProcess = proc;
	proc->setProcessChannelMode(QProcess::MergedChannels);
	QObject::connect(proc, &QProcess::readyReadStandardOutput, [proc]() {
		QString output = proc->readAllStandardOutput();
		LogAlert(output.trimmed());
	});
	QObject::connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),
		[proc](int code, QProcess::ExitStatus) {
		LogAlert(QString("Alerts backend exited (code %1)").arg(code));
		alertsRunning = false;
		alertsProcess = nullptr;
		connStatus->setText("Status: Disconnected");
		connStatus->setStyleSheet("color: #e44;");
		connectBtn->setEnabled(true);
		disconnectBtn->setEnabled(false);
		proc->deleteLater();
	});

	/* Add obs-agent venv to PATH for dependencies */
	const char *agentPath = obs_get_module_binary_path(obs_get_module("streamdirector-agent"));
	if (agentPath) {
		QString agentDir = QFileInfo(QString::fromUtf8(agentPath)).absolutePath();
		/* Try to find python in the venv */
		QString venvPython = agentDir + "/../../../obs-agent/venv/Scripts/python.exe";
		if (QFile::exists(venvPython)) {
			pythonExe = venvPython;
		}
	}

	proc->start(pythonExe, args);
	alertsRunning = true;
	connStatus->setText("Status: Connecting...");
	connStatus->setStyleSheet("color: #ea4;");
	connectBtn->setEnabled(false);
	disconnectBtn->setEnabled(true);
	LogAlert("Alerts backend started. Connecting to Twitch EventSub...");

	/* Check connection after a few seconds */
	QTimer::singleShot(5000, []() {
		if (alertsRunning) {
			connStatus->setText("Status: Connected");
			connStatus->setStyleSheet("color: #4a9;");
		}
	});
}

static void StopAlertsBackend()
{
	if (!alertsRunning) {
		LogAlert("Alerts backend not running.");
		return;
	}

	/* The Python script listens for a shutdown signal - we write a stop file */
	QString stopFile = GetConfigDir() + "/STOP";
	QFile f(stopFile);
	if (f.open(QIODevice::WriteOnly)) {
		f.write("stop");
		f.close();
	}

	/* Also forcefully kill the process after a short delay */
	if (alertsProcess) {
		QTimer::singleShot(2000, []() {
			if (alertsProcess) {
				alertsProcess->kill();
				alertsProcess->deleteLater();
				alertsProcess = nullptr;
			}
		});
	}

	LogAlert("Stopping alerts backend...");
	alertsRunning = false;
	connStatus->setText("Status: Stopping...");
	connStatus->setStyleSheet("color: #ea4;");

	/* Clean up stop file after a moment */
	QTimer::singleShot(3000, [stopFile]() {
		QFile::remove(stopFile);
		connStatus->setText("Status: Disconnected");
		connStatus->setStyleSheet("color: #e44;");
		connectBtn->setEnabled(true);
		disconnectBtn->setEnabled(false);
	});
}

static void LoginWithTwitch()
{
	if (loginInProgress) {
		LogAlert("Login already in progress...");
		return;
	}

	QString pythonScript;
	char *scriptPath = obs_module_file("sd_alerts.py");
	if (scriptPath) {
		pythonScript = QString::fromUtf8(scriptPath);
		bfree(scriptPath);
	}

	if (pythonScript.isEmpty()) {
		LogAlert("ERROR: sd_alerts.py not found.");
		return;
	}

	QString pythonExe = "python";
	const char *agentPath = obs_get_module_binary_path(obs_get_module("streamdirector-agent"));
	if (agentPath) {
		QString agentDir = QFileInfo(QString::fromUtf8(agentPath)).absolutePath();
		QString venvPython = agentDir + "/../../../obs-agent/venv/Scripts/python.exe";
		if (QFile::exists(venvPython)) {
			pythonExe = venvPython;
		}
	}

	loginInProgress = true;
	loginBtn->setEnabled(false);
	loginBtn->setText("Logging in...");
	LogAlert("Starting Twitch login (device code flow)...");

	QStringList args;
	args << pythonScript << "--login";

	QProcess *proc = new QProcess();
	proc->setProcessChannelMode(QProcess::MergedChannels);

	QObject::connect(proc, &QProcess::readyReadStandardOutput, [proc]() {
		QString output = proc->readAllStandardOutput();
		LogAlert(output.trimmed());
	});

	QObject::connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),
		[proc](int code, QProcess::ExitStatus) {
		loginInProgress = false;
		loginBtn->setEnabled(true);
		loginBtn->setText("Login with Twitch");

		if (code == 0) {
			/* Read the tokens file that the Python script wrote */
			QString accessToken = LoadAccessToken();
			QString refreshToken = LoadRefreshToken();
			if (!accessToken.isEmpty()) {
				tokenEdit->setText(accessToken);
				LogAlert("Login successful! Tokens saved. Click Connect to start.");
				connStatus->setText("Status: Ready to connect");
				connStatus->setStyleSheet("color: #4a9;");
			} else {
				LogAlert("Login completed but no token found. Check log for errors.");
			}
		} else {
			LogAlert(QString("Login failed (code %1). Check log above.").arg(code));
		}
		proc->deleteLater();
	});

	proc->start(pythonExe, args);
}

/* ===== Alert Configuration UI ===== */

struct AlertConfigFields {
	QCheckBox *enabled;
	QLineEdit *title;
	QLineEdit *message;
	QLineEdit *emoji;
	QLineEdit *image;
	QLineEdit *sound;
	QLineEdit *color;
	QSpinBox *duration;
	QPushButton *testBtn;
};

struct AlertTypeInfo {
	const char *eventType;
	const char *displayName;
	const char *defaultTitle;
	const char *defaultMessage;
	const char *defaultEmoji;
	const char *defaultColor;
	int defaultDuration;
};

static const AlertTypeInfo ALERT_TYPES[] = {
	{"channel.follow", "Follow", "New Follower!", "Welcome {user_name}!", "❤️", "#9b59b6", 6000},
	{"channel.cheer", "Cheer (Bits)", "{user_name} cheered {bits} bits!", "Thanks for the bits!", "👏", "#f39c12", 5000},
	{"channel.subscribe", "Subscribe", "New Subscriber!", "Thanks for subscribing, {user_name}!", "⭐", "#3498db", 8000},
	{"channel.subscription.message", "Resubscribe", "{user_name} resubscribed!", "{cumulative_months} months strong!", "🔥", "#3498db", 8000},
	{"channel.subscription.gift", "Gift Sub", "{user_name} gifted {gift_count} subs!", "What generosity!", "🎁", "#2ecc71", 8000},
	{"channel.raid", "Raid", "{from_broadcaster_user_name} is raiding!", "with {viewers} raiders!", "🚗", "#e74c3c", 8000},
	{"channel.channel_points_custom_reward_redemption.add", "Channel Points", "{user_name} redeemed a reward!", "{reward_title}", "🏆", "#f1c40f", 5000},
};
static const int NUM_ALERT_TYPES = sizeof(ALERT_TYPES) / sizeof(ALERT_TYPES[0]);

static QMap<QString, AlertConfigFields> configFields;
static QTabWidget *configTabs = nullptr;

static QJsonObject LoadAlertConfigJson()
{
	QString path = GetConfigDir() + "/alerts_config.json";
	QFile f(path);
	if (f.open(QIODevice::ReadOnly | QIODevice::Text)) {
		return QJsonDocument::fromJson(f.readAll()).object();
	}
	/* Try default config from plugin data */
	char *defaultPath = obs_module_file("alerts_config.json");
	if (defaultPath) {
		QFile df(QString::fromUtf8(defaultPath));
		bfree(defaultPath);
		if (df.open(QIODevice::ReadOnly | QIODevice::Text)) {
			return QJsonDocument::fromJson(df.readAll()).object();
		}
	}
	return QJsonObject();
}

static void SaveAlertConfigJson(const QJsonObject &config)
{
	QString path = GetConfigDir() + "/alerts_config.json";
	QJsonDocument doc(config);
	QFile f(path);
	if (f.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
		f.write(doc.toJson(QJsonDocument::Indented));
	}
}

static void SendTestAlert(const QString &eventType)
{
	/* Send a test alert directly via C++ WebSocket client (no Python needed) */
	QTcpSocket *sock = new QTcpSocket();
	QObject::connect(sock, &QTcpSocket::connected, [sock, eventType]() {
		/* Generate 16 random bytes and base64-encode for Sec-WebSocket-Key */
		QByteArray keyBytes(16, 0);
		for (int i = 0; i < 16; i++)
			keyBytes[i] = char(QRandomGenerator::global()->generate() & 0xFF);
		QString key = QString::fromLatin1(keyBytes.toBase64());

		/* Send WebSocket upgrade request */
		QString handshake = QString(
			"GET / HTTP/1.1\r\n"
			"Host: 127.0.0.1:9191\r\n"
			"Upgrade: websocket\r\n"
			"Connection: Upgrade\r\n"
			"Sec-WebSocket-Key: %1\r\n"
			"Sec-WebSocket-Version: 13\r\n"
			"\r\n"
		).arg(key);
		sock->write(handshake.toUtf8());
	});

	QObject::connect(sock, &QTcpSocket::readyRead, [sock, eventType]() {
		/* Read the upgrade response */
		QByteArray resp = sock->readAll();
		if (!resp.contains("101")) {
			LogAlert("Test alert: WebSocket upgrade failed.");
			sock->close();
			return;
		}

		/* Build the alert JSON */
		QJsonObject data;
		data["event_type"] = eventType;
		data["user_name"] = "TestUser";
		data["bits"] = "100";
		data["viewers"] = "5";
		data["cumulative_months"] = "12";
		data["gift_count"] = "3";
		data["from_broadcaster_user_name"] = "TestRaider";
		data["reward_title"] = "Test Reward";

		QJsonObject msg;
		msg["type"] = "alert";
		msg["data"] = data;

		QByteArray payload = QJsonDocument(msg).toJson(QJsonDocument::Compact);

		/* Build WebSocket frame: FIN + text opcode (0x81), masked (0x80) */
		QByteArray frame;
		frame.append(char(0x81));

		/* Payload length with mask bit */
		if (payload.size() < 126) {
			frame.append(char(0x80 | payload.size()));
		} else if (payload.size() < 65536) {
			frame.append(char(0x80 | 126));
			frame.append(char((payload.size() >> 8) & 0xFF));
			frame.append(char(payload.size() & 0xFF));
		} else {
			frame.append(char(0x80 | 127));
			for (int i = 7; i >= 0; i--)
				frame.append(char((payload.size() >> (i * 8)) & 0xFF));
		}

		/* Masking key (4 random bytes) */
		QByteArray mask(4, 0);
		for (int i = 0; i < 4; i++)
			mask[i] = char(QRandomGenerator::global()->generate() & 0xFF);
		frame.append(mask);

		/* Masked payload */
		for (int i = 0; i < payload.size(); i++)
			frame.append(payload[i] ^ mask[i % 4]);

		sock->write(frame);
		sock->flush();
		sock->close();

		LogAlert(QString("Test alert sent: %1").arg(eventType));
	});

	QObject::connect(sock, &QTcpSocket::errorOccurred, [sock](QAbstractSocket::SocketError) {
		LogAlert("Test alert: Could not connect to backend. Is it running?");
		sock->deleteLater();
	});

	QObject::connect(sock, &QTcpSocket::disconnected, [sock]() {
		sock->deleteLater();
	});

	sock->connectToHost("127.0.0.1", 9191);
}

static void SaveConfigFromFields()
{
	QJsonObject config = LoadAlertConfigJson();
	QJsonObject alerts = config.value("alerts").toObject();

	for (int i = 0; i < NUM_ALERT_TYPES; i++) {
		const AlertTypeInfo &info = ALERT_TYPES[i];
		QString key = QString::fromUtf8(info.eventType);
		if (!configFields.contains(key))
			continue;

		const AlertConfigFields &f = configFields[key];
		QJsonObject alert;
		alert["enabled"] = f.enabled->isChecked();
		alert["title"] = f.title->text();
		alert["message"] = f.message->text();
		alert["image"] = f.image->text();
		alert["emoji"] = f.emoji->text();
		alert["color"] = f.color->text();
		alert["sound"] = f.sound->text();
		alert["duration"] = f.duration->value();
		alerts[key] = alert;
	}

	config["alerts"] = alerts;
	SaveAlertConfigJson(config);
}

static QWidget *BuildConfigureTab(QWidget *parent)
{
	QWidget *tab = new QWidget(parent);
	QVBoxLayout *tabLayout = new QVBoxLayout(tab);
	tabLayout->setContentsMargins(4, 4, 4, 4);

	/* Scroll area for alert configs */
	QScrollArea *scroll = new QScrollArea(tab);
	scroll->setWidgetResizable(true);
	QWidget *scrollContent = new QWidget(scroll);
	QVBoxLayout *alertsLayout = new QVBoxLayout(scrollContent);
	alertsLayout->setContentsMargins(4, 4, 4, 4);
	alertsLayout->setSpacing(8);

	QJsonObject config = LoadAlertConfigJson();
	QJsonObject alerts = config.value("alerts").toObject();

	for (int i = 0; i < NUM_ALERT_TYPES; i++) {
		const AlertTypeInfo &info = ALERT_TYPES[i];
		QString key = QString::fromUtf8(info.eventType);

		QJsonObject alertObj = alerts.value(key).toObject();

		QGroupBox *group = new QGroupBox(QString::fromUtf8(info.displayName), scrollContent);
		QFormLayout *form = new QFormLayout(group);
		form->setContentsMargins(8, 8, 8, 8);

		AlertConfigFields f;

		f.enabled = new QCheckBox("Enabled", group);
		f.enabled->setChecked(alertObj.value("enabled").toBool(true));
		form->addRow("", f.enabled);

		f.title = new QLineEdit(group);
		f.title->setText(alertObj.value("title").toString(QString::fromUtf8(info.defaultTitle)));
		form->addRow("Title:", f.title);

		f.message = new QLineEdit(group);
		f.message->setText(alertObj.value("message").toString(QString::fromUtf8(info.defaultMessage)));
		form->addRow("Message:", f.message);

		f.emoji = new QLineEdit(group);
		f.emoji->setText(alertObj.value("emoji").toString(QString::fromUtf8(info.defaultEmoji)));
		f.emoji->setMaximumWidth(60);
		form->addRow("Emoji:", f.emoji);

		/* Image file picker */
		f.image = new QLineEdit(group);
		f.image->setText(alertObj.value("image").toString(""));
		f.image->setPlaceholderText("Path to image file (optional)...");
		QPushButton *imageBrowse = new QPushButton("...", group);
		imageBrowse->setMaximumWidth(30);
		QHBoxLayout *imageLayout = new QHBoxLayout();
		imageLayout->addWidget(f.image);
		imageLayout->addWidget(imageBrowse);
		QWidget *imageContainer = new QWidget(group);
		imageContainer->setLayout(imageLayout);
		form->addRow("Image:", imageContainer);

		/* Sound file picker */
		f.sound = new QLineEdit(group);
		f.sound->setText(alertObj.value("sound").toString(""));
		f.sound->setPlaceholderText("Path to sound file (optional)...");
		QPushButton *soundBrowse = new QPushButton("...", group);
		soundBrowse->setMaximumWidth(30);
		QHBoxLayout *soundLayout = new QHBoxLayout();
		soundLayout->addWidget(f.sound);
		soundLayout->addWidget(soundBrowse);
		QWidget *soundContainer = new QWidget(group);
		soundContainer->setLayout(soundLayout);
		form->addRow("Sound:", soundContainer);

		/* Color picker */
		f.color = new QLineEdit(group);
		f.color->setText(alertObj.value("color").toString(QString::fromUtf8(info.defaultColor)));
		f.color->setMaximumWidth(80);
		QPushButton *colorPick = new QPushButton("Pick", group);
		colorPick->setMaximumWidth(50);
		QHBoxLayout *colorLayout = new QHBoxLayout();
		colorLayout->addWidget(f.color);
		colorLayout->addWidget(colorPick);
		colorLayout->addStretch();
		QWidget *colorContainer = new QWidget(group);
		colorContainer->setLayout(colorLayout);
		form->addRow("Color:", colorContainer);

		/* Duration */
		f.duration = new QSpinBox(group);
		f.duration->setRange(1000, 30000);
		f.duration->setSingleStep(500);
		f.duration->setSuffix(" ms");
		f.duration->setValue(alertObj.value("duration").toInt(info.defaultDuration));
		form->addRow("Duration:", f.duration);

		/* Test button */
		f.testBtn = new QPushButton("Test Alert", group);
		form->addRow("", f.testBtn);

		/* Connections */
		QString eventType = key;
		QLineEdit *imageField = f.image;
		QLineEdit *soundField = f.sound;
		QLineEdit *colorField = f.color;

		QObject::connect(imageBrowse, &QPushButton::clicked, [parent, imageField]() {
			QString path = QFileDialog::getOpenFileName(parent, "Select Image", "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.svg)");
			if (!path.isEmpty())
				imageField->setText(path);
		});

		QObject::connect(soundBrowse, &QPushButton::clicked, [parent, soundField]() {
			QString path = QFileDialog::getOpenFileName(parent, "Select Sound", "", "Audio (*.mp3 *.wav *.ogg *.m4a)");
			if (!path.isEmpty())
				soundField->setText(path);
		});

		QObject::connect(colorPick, &QPushButton::clicked, [parent, colorField]() {
			QColor current(colorField->text());
			QColor chosen = QColorDialog::getColor(current.isValid() ? current : Qt::white, parent, "Pick Alert Color");
			if (chosen.isValid())
				colorField->setText(chosen.name());
		});

		QObject::connect(f.testBtn, &QPushButton::clicked, [eventType]() {
			SaveConfigFromFields();
			SendTestAlert(eventType);
		});

		/* Auto-save on field changes */
		auto saveOnEdit = [eventType](const QString &) {
			Q_UNUSED(eventType)
			SaveConfigFromFields();
		};
		QObject::connect(f.title, &QLineEdit::editingFinished, []() { SaveConfigFromFields(); });
		QObject::connect(f.message, &QLineEdit::editingFinished, []() { SaveConfigFromFields(); });
		QObject::connect(f.emoji, &QLineEdit::editingFinished, []() { SaveConfigFromFields(); });
		QObject::connect(f.image, &QLineEdit::editingFinished, []() { SaveConfigFromFields(); });
		QObject::connect(f.sound, &QLineEdit::editingFinished, []() { SaveConfigFromFields(); });
		QObject::connect(f.color, &QLineEdit::editingFinished, []() { SaveConfigFromFields(); });
		QObject::connect(f.duration, QOverload<int>::of(&QSpinBox::valueChanged), [](int) { SaveConfigFromFields(); });
		QObject::connect(f.enabled, &QCheckBox::toggled, [](bool) { SaveConfigFromFields(); });

		configFields[key] = f;
		alertsLayout->addWidget(group);
	}

	alertsLayout->addStretch();
	scroll->setWidget(scrollContent);
	tabLayout->addWidget(scroll);

	/* Save button at bottom */
	QPushButton *saveBtn = new QPushButton("Save Configuration", tab);
	tabLayout->addWidget(saveBtn);
	QObject::connect(saveBtn, &QPushButton::clicked, []() {
		SaveConfigFromFields();
		LogAlert("Alert configuration saved.");
	});

	return tab;
}

bool obs_module_load(void)
{
	blog(LOG_INFO, "[streamdirector-alerts] Loading StreamDirector Alerts plugin");

	cef = obs_browser_init_panel();
	if (cef) {
		if (!cef->init_browser()) {
			blog(LOG_INFO, "[streamdirector-alerts] Waiting for CEF browser init...");
			cef->wait_for_browser_init();
		}
		blog(LOG_INFO, "[streamdirector-alerts] CEF browser initialized.");
	} else {
		blog(LOG_WARNING, "[streamdirector-alerts] CEF not available");
	}

	std::string htmlUrl = GetAlertsHtmlPath();
	blog(LOG_INFO, "[streamdirector-alerts] Alerts HTML URL: %s", htmlUrl.c_str());

	QWidget *mainWidget = new QWidget((QWidget *)obs_frontend_get_main_window());
	mainWidget->setObjectName("StreamDirectorAlertsWidget");

	QVBoxLayout *mainLayout = new QVBoxLayout(mainWidget);
	mainLayout->setContentsMargins(4, 4, 4, 4);

	/* Settings section */
	QGroupBox *settingsGroup = new QGroupBox("Twitch Connection", mainWidget);
	QVBoxLayout *settingsLayout = new QVBoxLayout(settingsGroup);

	QLabel *tokenLabel = new QLabel("OAuth Token:", settingsGroup);
	tokenEdit = new QLineEdit(settingsGroup);
	tokenEdit->setPlaceholderText("Click 'Login with Twitch' to authenticate...");
	tokenEdit->setEchoMode(QLineEdit::Password);

	QString savedToken = LoadAccessToken();
	if (!savedToken.isEmpty()) {
		tokenEdit->setText(savedToken);
	}

	QHBoxLayout *btnLayout = new QHBoxLayout();
	loginBtn = new QPushButton("Login with Twitch", settingsGroup);
	connectBtn = new QPushButton("Connect", settingsGroup);
	disconnectBtn = new QPushButton("Disconnect", settingsGroup);
	disconnectBtn->setEnabled(false);
	QPushButton *addSourceBtn = new QPushButton("Add to Scene", settingsGroup);

	btnLayout->addWidget(loginBtn);
	btnLayout->addWidget(connectBtn);
	btnLayout->addWidget(disconnectBtn);
	btnLayout->addStretch();
	btnLayout->addWidget(addSourceBtn);

	settingsLayout->addWidget(tokenLabel);
	settingsLayout->addWidget(tokenEdit);
	settingsLayout->addLayout(btnLayout);

	/* Connection status */
	connStatus = new QLabel("Status: Disconnected", settingsGroup);
	connStatus->setStyleSheet("color: #e44; padding: 2px;");
	settingsLayout->addWidget(connStatus);

	/* Help text */
	QLabel *helpLabel = new QLabel(
		"<small>Click 'Login with Twitch' to authenticate via device code flow. "
		"Token auto-refreshes when connected.</small>", settingsGroup);
	helpLabel->setWordWrap(true);
	helpLabel->setOpenExternalLinks(true);
	helpLabel->setTextFormat(Qt::RichText);
	settingsLayout->addWidget(helpLabel);

	mainLayout->addWidget(settingsGroup);

	/* Tabbed area: alerts preview + log */
	QTabWidget *tabs = new QTabWidget(mainWidget);

	/* Alerts display tab (CEF browser) */
	if (cef && !htmlUrl.empty()) {
		QWidget *browserTab = new QWidget(tabs);
		QVBoxLayout *browserLayout = new QVBoxLayout(browserTab);
		browserLayout->setContentsMargins(0, 0, 0, 0);

		cefWidget = cef->create_widget(browserTab, htmlUrl);
		if (cefWidget) {
			browserLayout->addWidget(cefWidget);
		}
		tabs->addTab(browserTab, "Alerts");
	} else {
		QLabel *noBrowser = new QLabel("Browser panel not available.", tabs);
		noBrowser->setAlignment(Qt::AlignCenter);
		tabs->addTab(noBrowser, "Alerts");
	}

	/* Log tab */
	QWidget *logTab = new QWidget(tabs);
	QVBoxLayout *logLayout = new QVBoxLayout(logTab);
	logLayout->setContentsMargins(0, 0, 0, 0);
	alertsLog = new QTextEdit(logTab);
	alertsLog->setReadOnly(true);
	alertsLog->setStyleSheet("font-family: monospace; font-size: 11px;");
	alertsLog->setPlaceholderText("Alert events will appear here...");
	logLayout->addWidget(alertsLog);
	tabs->addTab(logTab, "Log");

	/* Configure tab */
	QWidget *configureTab = BuildConfigureTab(mainWidget);
	tabs->addTab(configureTab, "Configure");

	mainLayout->addWidget(tabs, 1);

	/* Connections */
	QObject::connect(loginBtn, &QPushButton::clicked, []() { LoginWithTwitch(); });
	QObject::connect(connectBtn, &QPushButton::clicked, []() { StartAlertsBackend(); });
	QObject::connect(disconnectBtn, &QPushButton::clicked, []() { StopAlertsBackend(); });

	QObject::connect(addSourceBtn, &QPushButton::clicked, [htmlUrl]() {
		/* Add a browser source with the alerts HTML URL */
		std::string url = htmlUrl;
		if (url.empty()) {
			char *htmlPath = obs_module_file("alerts.html");
			if (htmlPath) {
				url = std::string("file:///") + htmlPath;
				bfree(htmlPath);
			}
		}

		obs_source_t *source = obs_get_source_by_name("StreamDirector Alerts");
		bool isNew = false;

		if (!source) {
			source = obs_source_create("browser_source", "StreamDirector Alerts",
				nullptr, nullptr);
			isNew = true;
		}

		if (source) {
			if (isNew) {
				obs_data_t *settings = obs_source_get_settings(source);
				obs_data_set_string(settings, "url", url.c_str());
				obs_data_set_int(settings, "width", 800);
				obs_data_set_int(settings, "height", 600);
				obs_data_set_bool(settings, "shutdown", false);
				obs_source_update(source, settings);
				obs_data_release(settings);
			}

			/* Add to current scene */
			obs_source_t *currentScene = obs_frontend_get_current_scene();
			if (currentScene) {
				obs_scene_t *scene = obs_scene_from_source(currentScene);
				if (scene) {
					/* Check if already in this scene */
					obs_sceneitem_t *existing = obs_scene_find_source(scene, "StreamDirector Alerts");
					if (existing) {
						LogAlert("Alerts source already in this scene.");
					} else {
						obs_scene_add(scene, source);
						LogAlert("Alerts browser source added to current scene.");
					}
				}
				obs_source_release(currentScene);
			}

			obs_source_release(source);
		}
	});

	/* Create the dock */
	alertsDock = new QDockWidget("StreamDirector Alerts", (QWidget *)obs_frontend_get_main_window());
	alertsDock->setObjectName("StreamDirectorAlertsDock");
	alertsDock->setAllowedAreas(Qt::AllDockWidgetAreas);
	alertsDock->setTitleBarWidget(new QWidget());
	alertsDock->setWidget(mainWidget);

	obs_frontend_add_dock_by_id("StreamDirectorAlerts", "StreamDirector Alerts", alertsDock);

	blog(LOG_INFO, "[streamdirector-alerts] Plugin loaded successfully");
	return true;
}

void obs_module_unload(void)
{
	if (alertsProcess) {
		alertsProcess->kill();
		alertsProcess->deleteLater();
		alertsProcess = nullptr;
	}
	alertsRunning = false;

	if (alertsDock) {
		obs_frontend_remove_dock("StreamDirectorAlerts");
		delete alertsDock;
		alertsDock = nullptr;
	}

	blog(LOG_INFO, "[streamdirector-alerts] Plugin unloaded");
}
