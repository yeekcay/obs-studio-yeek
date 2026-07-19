#pragma once

#include <QDockWidget>
#include <QTextEdit>
#include <QLineEdit>
#include <QPushButton>
#include <QLabel>
#include <QThread>
#include <QTabWidget>
#include <QComboBox>
#include <string>

class AgentWorker : public QObject {
	Q_OBJECT

public:
	explicit AgentWorker(QObject *parent = nullptr);

	void setInstruction(const std::string &instruction);
	void setMode(const std::string &mode);

public slots:
	void run();

signals:
	void finished(const QString &result);
	void output(const QString &text);
	void log(const QString &text);

private:
	std::string instruction_;
	std::string mode_ = "responsive";
};

class AgentDock : public QDockWidget {
	Q_OBJECT

public:
	explicit AgentDock(QWidget *parent = nullptr);
	~AgentDock();

private slots:
	void onSendClicked();
	void onStopClicked();
	void onAgentFinished(const QString &result);
	void onAgentOutput(const QString &text);
	void onAgentLog(const QString &text);

private:
	QTabWidget *tabs_;
	QTextEdit *chatDisplay_;
	QTextEdit *logDisplay_;
	QLineEdit *inputField_;
	QPushButton *sendButton_;
	QPushButton *stopButton_;
	QLabel *statusLabel_;
	QComboBox *modeCombo_;
	QThread *workerThread_;
	AgentWorker *worker_;
};
