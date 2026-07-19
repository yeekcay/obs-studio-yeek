#include "AgentDock.hpp"
#include "PythonBridge.hpp"

#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QScrollBar>
#include <QDateTime>
#include <QMetaObject>

AgentWorker::AgentWorker(QObject *parent) : QObject(parent) {}

void AgentWorker::setInstruction(const std::string &instruction)
{
	instruction_ = instruction;
}

void AgentWorker::setMode(const std::string &mode)
{
	mode_ = mode;
}

void AgentWorker::run()
{
	emit log("Starting agent execution...");

	std::string result;
	PythonBridge::Instance().SetOutputCallback(
		[this](const std::string &text) { emit output(QString::fromStdString(text)); });

	bool success = PythonBridge::Instance().RunAgent(instruction_, result, mode_);

	if (success) {
		emit log("Agent completed successfully.");
	} else {
		emit log("Agent completed with errors.");
	}

	QString qResult = QString::fromStdString(result);
	emit finished(qResult);
}

AgentDock::AgentDock(QWidget *parent) : QDockWidget("StreamDirector Agent", parent)
{
	setObjectName("StreamDirectorAgentDock");
	setAllowedAreas(Qt::AllDockWidgetAreas);
	setTitleBarWidget(new QWidget());

	QWidget *container = new QWidget(this);
	QVBoxLayout *layout = new QVBoxLayout(container);
	layout->setContentsMargins(4, 4, 4, 4);

	statusLabel_ = new QLabel("Status: Idle", container);
	statusLabel_->setStyleSheet("padding: 4px; color: #888;");

	tabs_ = new QTabWidget(container);

	chatDisplay_ = new QTextEdit(container);
	chatDisplay_->setReadOnly(true);
	chatDisplay_->setPlaceholderText("Agent output will appear here...");

	logDisplay_ = new QTextEdit(container);
	logDisplay_->setReadOnly(true);
	logDisplay_->setPlaceholderText("Logs will appear here...");
	logDisplay_->setStyleSheet("font-family: monospace; font-size: 11px;");

	tabs_->addTab(chatDisplay_, "Chat");
	tabs_->addTab(logDisplay_, "Log");

	// Mode selector
	QHBoxLayout *modeLayout = new QHBoxLayout();
	QLabel *modeLabel = new QLabel("Mode:", container);
	modeCombo_ = new QComboBox(container);
	modeCombo_->addItem("Responsive", "responsive");
	modeCombo_->addItem("Autonomous Stream", "autonomous");
	modeCombo_->addItem("BitNet (Fast)", "bitnet");
	modeCombo_->setToolTip("Responsive: Only does what you ask.\nAutonomous Stream: Auto-manages scenes, audio, recording, and streaming.\nBitNet (Fast): Lightweight agent loop using local 1-bit LLM for fast tool calls.");
	modeLayout->addWidget(modeLabel);
	modeLayout->addWidget(modeCombo_);
	modeLayout->addStretch();

	inputField_ = new QLineEdit(container);
	inputField_->setPlaceholderText("Enter instruction for the agent...");

	sendButton_ = new QPushButton("Send", container);
	stopButton_ = new QPushButton("Stop", container);
	stopButton_->setEnabled(false);

	QHBoxLayout *inputLayout = new QHBoxLayout();
	inputLayout->addWidget(inputField_);
	inputLayout->addWidget(sendButton_);
	inputLayout->addWidget(stopButton_);

	layout->addWidget(statusLabel_);
	layout->addLayout(modeLayout);
	layout->addWidget(tabs_, 1);
	layout->addLayout(inputLayout);

	setWidget(container);

	workerThread_ = new QThread(this);
	worker_ = new AgentWorker();
	worker_->moveToThread(workerThread_);

	connect(sendButton_, &QPushButton::clicked, this, &AgentDock::onSendClicked);
	connect(inputField_, &QLineEdit::returnPressed, this, &AgentDock::onSendClicked);
	connect(stopButton_, &QPushButton::clicked, this, &AgentDock::onStopClicked);
	connect(worker_, &AgentWorker::finished, this, &AgentDock::onAgentFinished);
	connect(worker_, &AgentWorker::output, this, &AgentDock::onAgentOutput);
	connect(worker_, &AgentWorker::log, this, &AgentDock::onAgentLog);
	connect(workerThread_, &QThread::finished, worker_, &QObject::deleteLater);

	workerThread_->start();
}

AgentDock::~AgentDock()
{
	workerThread_->quit();
	workerThread_->wait();
}

void AgentDock::onSendClicked()
{
	QString text = inputField_->text().trimmed();
	if (text.isEmpty())
		return;

	if (PythonBridge::Instance().IsAgentRunning()) {
		chatDisplay_->append("[System] Agent is already running, please wait...");
		return;
	}

	QString timestamp = QDateTime::currentDateTime().toString("hh:mm:ss");
	chatDisplay_->append(QString("[%1] You: %2").arg(timestamp, text));
	onAgentLog(QString("[%1] User input: %2").arg(timestamp, text));

	inputField_->clear();
	sendButton_->setEnabled(false);
	stopButton_->setEnabled(true);
	statusLabel_->setText("Status: Thinking...");
	statusLabel_->setStyleSheet("padding: 4px; color: #4a9;");

	worker_->setInstruction(text.toStdString());
	worker_->setMode(modeCombo_->currentData().toString().toStdString());

	QMetaObject::invokeMethod(worker_, "run", Qt::QueuedConnection);
}

void AgentDock::onStopClicked()
{
	if (!PythonBridge::Instance().IsAgentRunning())
		return;

	chatDisplay_->append("[System] Stopping agent...");
	PythonBridge::Instance().StopAgent();
	stopButton_->setEnabled(false);
}

void AgentDock::onAgentFinished(const QString &result)
{
	QString timestamp = QDateTime::currentDateTime().toString("hh:mm:ss");
	chatDisplay_->append(QString("[%1] Agent: %2").arg(timestamp, result));

	QScrollBar *bar = chatDisplay_->verticalScrollBar();
	bar->setValue(bar->maximum());

	sendButton_->setEnabled(true);
	stopButton_->setEnabled(false);
	statusLabel_->setText("Status: Idle");
	statusLabel_->setStyleSheet("padding: 4px; color: #888;");
}

void AgentDock::onAgentOutput(const QString &text)
{
	QString timestamp = QDateTime::currentDateTime().toString("hh:mm:ss");
	chatDisplay_->append(QString("[%1] > %2").arg(timestamp, text));

	QScrollBar *bar = chatDisplay_->verticalScrollBar();
	bar->setValue(bar->maximum());
}

void AgentDock::onAgentLog(const QString &text)
{
	QString timestamp = QDateTime::currentDateTime().toString("hh:mm:ss");
	logDisplay_->append(QString("[%1] %2").arg(timestamp, text));

	QScrollBar *bar = logDisplay_->verticalScrollBar();
	bar->setValue(bar->maximum());
}
