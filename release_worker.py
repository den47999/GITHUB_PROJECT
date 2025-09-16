import os
import subprocess
import shutil
from PyQt6.QtCore import QThread, pyqtSignal


class ReleaseWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, project_path, repo_name, release_data, screenshot_path=None):
        super().__init__()
        self.project_path = project_path
        self.repo_name = repo_name
        self.release_data = release_data
        self.screenshot_path = screenshot_path

    def _run_command(self, command, cwd=None):
        if cwd is None:
            cwd = self.project_path
        self.log_signal.emit(f"Выполнение команды: {command} в {cwd}")
        process = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True, encoding='utf-8')
        if process.stdout:
            self.log_signal.emit(f"Stdout: {process.stdout.strip()}")
        if process.stderr:
            self.log_signal.emit(f"Stderr: {process.stderr.strip()}")
        if process.returncode != 0:
            raise Exception(f"Команда завершилась с ошибкой (код {process.returncode}): {command}")
        return process.stdout

    def run(self):
        try:
            self.log_signal.emit(f"Рабочий поток запущен для создания релиза '{self.release_data['tag']}' для репозитория '{self.repo_name}'.")

            # Подготавливаем скриншот, если он указан
            screenshot_to_upload = None
            if self.screenshot_path and os.path.exists(self.screenshot_path):
                screenshots_dir = os.path.join(self.project_path, "screenshots")
                if not os.path.exists(screenshots_dir):
                    os.makedirs(screenshots_dir)

                screenshot_filename = os.path.basename(self.screenshot_path)
                destination_path = os.path.join(screenshots_dir, screenshot_filename)

                # Проверяем, не является ли исходный файл тем же, что и конечный
                if not os.path.exists(destination_path) or not os.path.samefile(self.screenshot_path, destination_path):
                    shutil.copy2(self.screenshot_path, destination_path)
                
                screenshot_to_upload = destination_path
                
                # Добавляем скриншот в README.md
                self._add_screenshot_to_readme(destination_path)
                
                # Коммитим изменения в README.md
                self._commit_and_push_changes()

            # Формируем команду для создания релиза
            command = f"gh release create {self.release_data['tag']}"
            
            # Добавляем заголовок, если указан
            if self.release_data['title']:
                command += f" --title \"{self.release_data['title']}\""
                
            # Добавляем примечания, если указаны
            if self.release_data['notes']:
                notes_filename = "temp_release_notes.md"
                notes_file_path = os.path.join(self.project_path, notes_filename)
                with open(notes_file_path, "w", encoding="utf-8") as f:
                    f.write(self.release_data['notes'])
                command += f' --notes-file "{notes_filename}"'
                
            # Добавляем скриншот, если он существует и не слишком большой
            if screenshot_to_upload and os.path.exists(screenshot_to_upload):
                # Проверяем размер файла (GitHub ограничивает размер файла в 2 ГБ)
                file_size = os.path.getsize(screenshot_to_upload)
                if file_size <= 2 * 1024 * 1024 * 1024:  # 2 ГБ
                    # Используем относительный путь для команды gh
                    relative_screenshot_path = os.path.relpath(screenshot_to_upload, self.project_path)
                    command += f' "{relative_screenshot_path}"'
                else:
                    self.log_signal.emit("Файл скриншота слишком большой для прикрепления к релизу")
            
            # Выполняем команду
            self._run_command(command)
            
            # Удаляем временный файл с примечаниями, если он был создан
            notes_file_path = os.path.join(self.project_path, "temp_release_notes.md")
            if os.path.exists(notes_file_path):
                os.remove(notes_file_path)
                
            self.log_signal.emit(f"Релиз '{self.release_data['tag']}' успешно создан для репозитория '{self.repo_name}'.")
            
            self.finished_signal.emit()
        except Exception as e:
            self.error_signal.emit(f"Произошла ошибка в рабочем потоке создания релиза: {e}")
            self.finished_signal.emit()

    def _commit_and_push_changes(self):
        """Коммитит и пушит изменения в репозиторий"""
        try:
            # Добавляем измененный README.md и скриншоты
            self._run_command("git add README.md screenshots")
            
            # Создаем коммит
            self._run_command('git commit -m "Добавлен скриншот в README.md"')
            
            # Пушим изменения
            self._run_command("git push origin master")
            
            self.log_signal.emit("Изменения в README.md закоммичены и отправлены в репозиторий")
        except Exception as e:
            self.log_signal.emit(f"Ошибка при коммите и пушу изменений: {e}")

    def _add_screenshot_to_readme(self, screenshot_path):
        """Добавляет скриншот в README.md"""
        readme_path = os.path.join(self.project_path, "README.md")
        
        if not os.path.exists(readme_path):
            self.log_signal.emit("README.md не найден, скриншот не будет добавлен.")
            return
            
        try:
            # Читаем содержимое README.md
            with open(readme_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            # Проверяем, есть ли уже раздел со скриншотами
            if "## 📸 Скриншоты" in content or "![Скриншот программы]" in content:
                self.log_signal.emit("Раздел со скриншотами уже существует в README.md.")
                # Удаляем существующий раздел, чтобы избежать дублирования
                content = content.split("## 📸 Скриншоты")[0]

            # Создаем относительный путь к скриншоту
            screenshot_filename = os.path.basename(screenshot_path)
            relative_screenshot_path = f"screenshots/{screenshot_filename}"
            
            # Создаем markdown для изображения
            screenshot_markdown = f"\n## 📸 Скриншоты\n\n![Скриншот программы]({relative_screenshot_path})\n"
            
            # Вставляем скриншот после краткого описания
            if "## Краткое описание проекта" in content:
                content = content.replace("## Краткое описание проекта", f"## Краткое описание проекта\n{screenshot_markdown}")
            else:
                # Если заголовок не найден, вставляем в начало файла
                content = screenshot_markdown + content

            # Записываем обновленное содержимое
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(content)
                
            self.log_signal.emit("Скриншот добавлен в README.md")
        except Exception as e:
            self.log_signal.emit(f"Ошибка при добавлении скриншота в README.md: {e}")