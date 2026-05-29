from django.test import TestCase
from django.contrib.auth.models import User
from pinterest_app.models import PinterestAccount, PinterestBoard
from rest_framework.test import APIClient
from rest_framework import status
import uuid

class PinterestAppTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(username='user1', password='password')
        self.user2 = User.objects.create_user(username='user2', password='password')
        self.admin = User.objects.create_superuser(username='admin', password='password')

        self.acc1 = PinterestAccount.objects.create(
            user=self.user1,
            name="Acc 1",
            cookies=[{"name": "test", "value": "123"}],
            proxy="http://proxy1"
        )
        self.acc2 = PinterestAccount.objects.create(
            user=self.user2,
            name="Acc 2",
            cookies=[{"name": "test", "value": "456"}],
            proxy="http://proxy2"
        )

        self.board1 = PinterestBoard.objects.create(
            account=self.acc1,
            board_id="p1",
            name="Board 1",
            url="/b1/"
        )

    def test_multi_tenancy_api(self):
        client = APIClient()
        client.force_authenticate(user=self.user1)

        # User 1 should only see their own account
        response = client.get('/api/v1/accounts/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Acc 1")

        # User 2 should only see their own
        client.force_authenticate(user=self.user2)
        response = client.get('/api/v1/accounts/')
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Acc 2")

        # Admin should see everything
        client.force_authenticate(user=self.admin)
        response = client.get('/api/v1/accounts/')
        self.assertEqual(len(response.data), 2)

    def test_webhook_access(self):
        client = APIClient()
        # Public access via token
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        # We don't have valid cookies/proxy so it will likely return error but should find the account
        response = client.post(url, {
            "image_url": "http://image.jpg",
            "board_name": "Board 1"
        }, format='json')

        # New logic returns 202 Accepted
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn('task_id', response.data)

    def test_board_refresh_mock(self):
        # This tests if the action is reachable
        client = APIClient()
        client.force_authenticate(user=self.user1)
        response = client.get(f'/api/v1/accounts/{self.acc1.id}/boards/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Board 1")
