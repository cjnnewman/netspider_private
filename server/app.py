import gevent.monkey

import json

gevent.monkey.patch_all()

import threading
import traceback
import time  # Add this import
import socket
import psutil
from flask import Flask, jsonify
from flask_socketio import SocketIO
from flask_cors import CORS
from Backend.Scraper import MegapersonalsScraper, SkipthegamesScraper, YesbackpageScraper, EscortalligatorScraper, \
    ErosScraper, RubratingsScraper
from Backend.resultManager.appendResults import FolderAppender, FolderAppender
from Backend.resultManager.resultManager import ResultManager
from PyQt5.QtWidgets import QFileDialog, QApplication
import subprocess
import sys
import os
import webbrowser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


app = Flask(__name__)
qt_app = QApplication([])
CORS(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")

'''
    ---------------------------------
    Manage Scraper and threads
    ---------------------------------
'''


class DirectoryWatchHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        print(f"Event triggered: {event}")  # Debugging line
        print("Updating files list...")  # Debugging line
        resultManager.update_folders_json()
        # resultList = resultManager.get_folders()
        #
        # # Check if resultList is not empty and send the list
        # if resultList:
        #     socketio.emit('result_folder_selected', {'folders': resultList})
        # else:
        #     # Notify if the directory is empty or there are no folders
        #     socketio.emit('result_folder_selected', {'error': 'No folders found in the selected directory'})




class ScraperManager:
    def __init__(self):
        self.scraper_thread = None
        self._lock = threading.Lock()
        self._active_threads = set()
        self._cleanup_timeout = 10  # seconds

    def start_scraper(self, kwargs):
        with self._lock:
            self.cleanup_dead_threads()
            if self.scraper_thread and self.scraper_thread.is_alive():
                print(f"Stopping existing thread {self.scraper_thread.ident}")
                self.manage_stop_scraper()
                self.force_stop_thread(max_attempts=3)
            
            self.scraper_thread = ScraperThread(kwargs)
            self._active_threads.add(self.scraper_thread)
            self.scraper_thread.start()
            print(f"Started thread id {self.scraper_thread.ident}")
            return {"Response": "Scraper Thread Started"}

    def manage_stop_scraper(self):
        with self._lock:
            if self.scraper_thread and self.scraper_thread.is_alive():
                try:
                    print(f"Stopping thread {self.scraper_thread.ident}")
                    self.scraper_thread.stop_thread()
                    self.scraper_thread.join_with_timeout()
                    if self.scraper_thread.is_alive():
                        self.force_stop_thread()
                finally:
                    if self.scraper_thread in self._active_threads:
                        self._active_threads.remove(self.scraper_thread)
                    self.cleanup_dead_threads()
                    remaining_threads = [t for t in threading.enumerate() if t != threading.main_thread()]
                    print(f"Active threads: {len(self._active_threads)}")
                    print(f"System threads: {len(remaining_threads)}")
                    print("Thread names:", [t.name for t in remaining_threads])

    def force_stop_thread(self, max_attempts=3):
        if self.scraper_thread:
            for attempt in range(max_attempts):
                try:
                    if attempt == 0:
                        self._graceful_shutdown()
                    elif attempt == 1:
                        self._kill_browser_processes()
                    else:
                        self._force_terminate_thread()
                    
                    if not self.scraper_thread.is_alive():
                        break
                except Exception as e:
                    print(f"Force stop attempt {attempt + 1} failed: {e}")
            
            # Remove thread from active_threads before setting to None
            if self.scraper_thread in self._active_threads:
                self._active_threads.remove(self.scraper_thread)
            self.scraper_thread = None

    def _graceful_shutdown(self):
        try:
            if hasattr(self.scraper_thread.scraper, 'driver') and self.scraper_thread.scraper.driver is not None:
                print("Trying to quit driver ...")
                try:
                    self.scraper_thread.scraper.driver.quit()
                    self.scraper_thread.scraper.driver = None
                    print("Driver quit successfully in _graceful_shutdown.")
                except Exception as e:
                    print(f"Error quitting driver in _graceful_shutdown: {e}")
            else:
                print("Driver is already None in _graceful_shutdown.")

            # Call stop_scraper for additional cleanup
            self.scraper_thread.scraper.stop_scraper()
            print("stop_scraper executed successfully.")

            # Signal and wait for thread termination
            self.scraper_thread._stop_event.set()
            print("Stop event set.")
            self.scraper_thread.join(timeout=2)
            print("Thread joined successfully.")
        except Exception as e:
            print(f"Graceful shutdown failed: {e}")
            with open("shutdown_traceback.log", "a") as log_file:
                traceback.print_exc(file=log_file)  # Logs the traceback to a file

    def _kill_browser_processes(self):
        """Kill any remaining browser processes"""
        try:
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if any(browser in proc.info['name'].lower() 
                          for browser in ['chrome', 'chromedriver']):
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            print(f"Error killing browser processes: {e}")

    def _force_terminate_thread(self):
        """Force terminate the thread as last resort"""
        if hasattr(self.scraper_thread, 'ident'):
            if sys.platform == 'win32':
                import ctypes
                try:
                    ctypes.windll.kernel32.TerminateThread(
                        ctypes.c_void_p(self.scraper_thread.ident), 0)
                except Exception as e:
                    print(f"Error force terminating thread: {e}")

    def cleanup_dead_threads(self):
        dead_threads = {thread for thread in self._active_threads if not thread.is_alive()}
        for thread in dead_threads:
            print(f"Cleaning up thread {thread.ident}")
            try:
                thread.cleanup_resources()
                thread.join(timeout=1)
                self._active_threads.remove(thread)
            except Exception as e:
                print(f"Error cleaning thread {thread.ident}: {e}")
            finally:
                # Ensure thread is removed even if cleanup fails
                if thread in self._active_threads:
                    self._active_threads.remove(thread)


class ScraperThread(threading.Thread):
    def __init__(self, kwargs):
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self.scraper = None
        self._init_scraper(kwargs)
        self._cleanup_lock = threading.Lock()
        self._cleanup_complete = False
        self._cleanup_attempted = False
        self.name = f"Scraper-{threading.current_thread().ident}"  # Give meaningful name

    def _init_scraper(self, kwargs):
        keywords = set(kwargs['keywords'].split(','))
        flagged_keywords = set(kwargs['flagged_keywords'].split(',')) if kwargs['flagged_keywords'] else set()
        
        if kwargs['website'] == 'eros':
            self.scraper = ErosScraper()
        elif kwargs['website'] == 'escortalligator':
            self.scraper = EscortalligatorScraper()
        elif kwargs['website'] == 'megapersonals':
            self.scraper = MegapersonalsScraper()
        elif kwargs['website'] == 'skipthegames':
            self.scraper = SkipthegamesScraper()
        elif kwargs['website'] == 'yesbackpage':
            self.scraper = YesbackpageScraper()
        elif kwargs['website'] == 'rubratings':
            self.scraper = RubratingsScraper()
        
        self.scraper.set_keywords(keywords)
        self.scraper.set_path(kwargs['path'])
        self.scraper.set_flagged_keywords(flagged_keywords)
        self.scraper.set_search_mode(kwargs['search_mode'])
        self.scraper.set_city(kwargs['city'])
        if kwargs['inclusive_search']:
            self.scraper.set_join_keywords()
        if kwargs['payment_methods_only']:
            self.scraper.set_only_posts_with_payment_methods()

        self.scraper._stop_event = self._stop_event  # Add this line

    def run(self):
        try:
            if not self._stop_event.is_set():
                print(f"Starting scraper thread id {self.ident}")
                socketio.emit('scraper_update', {'status': 'running'})
                self.scraper.initialize()
        except Exception as e:
            print(f"Error during scraper execution: {e}")
            socketio.emit('scraper_update', {'status': 'error', 'error': str(e)})
        finally:
            print("Cleaning up resources in run()...")
            self.cleanup_resources()
            socketio.emit('scraper_update', {'status': 'completed'})

    def cleanup_resources(self):
        print("Running cleanup_resources...")
        self.centralized_cleanup()

    def centralized_cleanup(self):
        with self._cleanup_lock:
            if self._cleanup_complete:
                print("Cleanup already completed.")
                return
            try:
                if self.scraper and getattr(self.scraper, 'driver', None):
                    print("Attempting to quit WebDriver in centralized_cleanup...")
                    self.scraper.driver.quit()
                    self.scraper.driver = None
                    print("Driver quit successfully in centralized_cleanup.")
            except Exception as e:
                print(f"Error in centralized_cleanup: {e}")
            finally:
                self._cleanup_complete = True
                self._stop_event.set()

    def stop_thread(self):
        """Stops the scraper thread gracefully."""
        try:
            if self._cleanup_attempted:
                return
            self._cleanup_attempted = True
            if self.scraper and not self.scraper.completed:
                self.scraper.stop_scraper()
            self.centralized_cleanup()
        except Exception as e:
            print(f"Error stopping scraper: {e}")
        finally:
            self._stop_event.set()

    def join_with_timeout(self, timeout=5):
        self._stop_event.set()
        start = time.time()
        while self.is_alive() and time.time() - start < timeout:
            time.sleep(0.1)
        if self.is_alive():
            print(f"Thread {self.ident} failed to join within timeout")

    def stopped(self):
        return self._stop_event.is_set()

# Defining Scraper Manager Obj for managing scraper and its thread
scraper_manager = ScraperManager()

'''
    ---------------------------------
    Result Manager functions
    ---------------------------------   
'''


def initialize_result_manager(result_dir):
    global resultManager
    resultManager = ResultManager(result_dir)
    resultManager.debug_print()

def initialize_folder_appender(result_dir):
    global folderAppend
    folderAppend = FolderAppender(result_dir)


'''
    ---------------------------------
    Socket Routes
    ---------------------------------
'''


# Connection Manager Sockets
@socketio.on('connection')
def connected():
    print("connected")


# Scraper Manager Sockets
@socketio.on('scraper_status')
def get_status():
    scraper_manager.get_scraper_status()


@socketio.on('start_scraper')
def start_scraper(data):
    socketio.emit('scraper_update', {'status': 'started'})
    print(data)
    response = scraper_manager.start_scraper(data)
    return {'Response': response}


@socketio.on('stop_scraper')
def stop_scraper():
    response = scraper_manager.manage_stop_scraper()
    socketio.emit('scraper_update', {'status': 'stopped'})
    return {'Response': response}


# Result Manager Sockets
@socketio.on('start_append')
def start_append(data):
    print(data)
    socketio.emit('result_manager_update', {'status': 'appending'})
    folderAppend.setSelectedFolders(data)
    folderAppend.create_new_folder()
    folderAppend.append_files()
    folderAppend.save_data()
    response = 0
    return {'Response': response}


@socketio.on('open_PDF')
def open_PDF(data):
    socketio.emit('result_manager_update', {'status': 'view_pdf'})
    print(data)
    response = resultManager.view_pdf(data)
    return {'Response': response}


@socketio.on('open_ss_dir')
def open_ss_dir(data):
    socketio.emit('result_manager_update', {'status': 'view_SS_dir'})
    print(data)
    response = resultManager.view_ss_dir(data)
    return {'Response': response}


@socketio.on('open_clean_data')
def open_clean_data(data):
    socketio.emit('result_manager_update', {'status': 'view_clean_data'})
    print(data)
    response = resultManager.view_clean_data(data)
    return {'Response': response}


@socketio.on('open_raw_data')
def open_raw_data(data):
    socketio.emit('result_manager_update', {'status': 'view_raw_data'})
    print(data)
    response = resultManager.view_raw_data(data)
    print(response)
    return {'Response': response}


@socketio.on('open_diagram_dir')
def open_diagram_dir(data):
    socketio.emit('result_manager_update', {'status': 'view_diagram_dir'})
    print(data)
    response = resultManager.view_diagram_dir(data)
    return {'Response': response}


@socketio.on('set_result_dir')
def set_result_dir():
    print("Selecting result directory")
    directory = QFileDialog.getExistingDirectory(None, "Select Directory", os.getcwd())
    print("Selected Directory: ", directory)

    print("Selected Directory: ", directory)
    result_dir = os.path.join(os.getcwd(), directory)

    # initialize the folder appender and result manager
    initialize_result_manager(result_dir)
    initialize_folder_appender(result_dir)

    # get the result list from result manager
    resultList = resultManager.get_folders()

    print(resultList)

    print("sending Result list")
    socketio.emit('result_folder_selected', {'folders': resultList, 'result_dir': result_dir})


@socketio.on('refresh_result_list')
def refresh_result_list():

    resultManager.update_folders_json()

    resultList = resultManager.get_folders()

    # Check if resultList is not empty and send the list
    if resultList:
        socketio.emit('result_list_refreshed', {'folders': resultList})
    else:
        # Notify if the directory is empty or there are no folders
        socketio.emit('result_list_refreshed', {'error': 'No folders found in the selected directory'})

@socketio.on_error_default
def handle_error(e):
    print(f"An error occurred: {str(e)}")
    socketio.emit('scraper_update', {'status': 'error', 'error': str(e)})
    response = {"error": str(e)}
    return response, 500


'''
    ---------------------------------
    Finding ports
    ---------------------------------
'''


def write_open_ports(ports):
    with open('open_ports.txt', 'w') as file:
        for port in ports:
            file.write(str(port) + '\n')


def find_open_port():
    print("Finding open port...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    print("Found open port:", port)
    return port


def find_open_ports(num):
    open_ports_list = []
    for _ in range(num):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            open_ports_list.append(s.getsockname()[1])
    return open_ports_list

def graceful_shutdown():
    print("Shutting down gracefully...")
    scraper_manager.manage_stop_scraper()
    print("All scrapers stopped. Cleaning up resources...")
    # Add any additional cleanup logic here
    print("Shutdown complete.")

#if __name__ == "__main__":
#    print("active threads: ", threading.active_count())
#
#    num_ports = 1  # Change this to the desired number of open ports
#    open_ports = find_open_ports(num_ports)
#
#    write_open_ports(open_ports)
#
#    print("Open Ports:", open_ports)
#
#    # Use the open ports as needed in the rest of your program
#    # Note: You may want to handle the case where `open_ports` is an empty list.
#    socketio.run(app, host='127.0.0.1', port=open_ports[0],
#                 allow_unsafe_werkzeug=True)

if __name__ == "__main__":
    try:
        print("Starting server...")
        num_ports = 1
        open_ports = find_open_ports(num_ports)
        write_open_ports(open_ports)
        print("Open Ports:", open_ports)
        socketio.run(app, host='127.0.0.1', port=open_ports[0], allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        graceful_shutdown()
    finally:
        print("Server has been stopped.")
