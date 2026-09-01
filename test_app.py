import unittest
from pathlib import Path

from textual.widgets import Input

from app import BrowseScreen, DatabasePicker, RolodexApp


class FakeStore:
    def __init__(self):
        self.searches = []

    def search(self, **filters):
        self.searches.append(filters)
        return [], 0

    def distinct_folders(self):
        return []


class TestBrowseScreen(BrowseScreen):
    def load_db(self):
        self.store = FakeStore()
        self.run_search()


class LiveSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_updates_after_typing(self):
        app = RolodexApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = TestBrowseScreen()
            app.push_screen(screen)
            await pilot.pause()

            screen.query_one("#query", Input).value = "alice"
            await pilot.pause(0.3)

            self.assertEqual(screen.store.searches[-1]["search"], "alice")

    async def test_database_picker_starts_in_working_directory(self):
        app = RolodexApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = TestBrowseScreen()
            app.push_screen(screen)
            await pilot.pause()

            await pilot.click("#choose_db")

            self.assertIsInstance(app.screen, DatabasePicker)
            self.assertEqual(app.screen.directory, Path.cwd())


if __name__ == "__main__":
    unittest.main()
