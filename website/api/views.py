import json
import smtplib
import uuid
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.db.models import Count, Q, Sum
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import filters, status, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAuthenticatedOrReadOnly
from rest_framework.response import Response
from rest_framework.views import APIView

from website.models import (
    ActivityLog,
    Contributor,
    Domain,
    Hunt,
    HuntPrize,
    InviteFriend,
    Issue,
    IssueScreenshot,
    Organization,
    Points,
    Project,
    Repo,
    Tag,
    TimeLog,
    Token,
    User,
    UserProfile,
)
from website.serializers import (
    ActivityLogSerializer,
    BugHuntPrizeSerializer,
    BugHuntSerializer,
    ContributorSerializer,
    DomainSerializer,
    IssueSerializer,
    OrganizationSerializer,
    ProjectSerializer,
    RepoSerializer,
    TagSerializer,
    TimeLogSerializer,
    UserProfileSerializer,
)
from website.utils import image_validator
from website.views.user import LeaderboardBase

# API's


class UserIssueViewSet(viewsets.ModelViewSet):
    """
    User Issue Model View Set
    """

    serializer_class = IssueSerializer
    filter_backends = (filters.SearchFilter,)
    search_fields = ("user__username", "user__id")
    http_method_names = ["get", "head"]

    def get_queryset(self):
        anonymous_user = self.request.user.is_anonymous
        user_id = self.request.user.id
        if anonymous_user:
            return Issue.objects.exclude(Q(is_hidden=True))
        else:
            return Issue.objects.exclude(Q(is_hidden=True) & ~Q(user_id=user_id))


class UserProfileViewSet(viewsets.ModelViewSet):
    """
    User Profile View Set
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfileSerializer
    queryset = UserProfile.objects.all()
    filter_backends = (filters.SearchFilter,)
    search_fields = ("id", "user__id", "user__username")
    http_method_names = ["get", "post", "head", "put"]

    def retrieve(self, request, pk, *args, **kwargs):
        user_profile = UserProfile.objects.filter(user__id=pk).first()

        if user_profile is None:
            return Response({"detail": "Not found."}, status=404)

        serializer = self.get_serializer(user_profile)
        return Response(serializer.data)

    def update(self, request, pk, *args, **kwargs):
        user_profile = request.user.userprofile

        if user_profile is None:
            return Response({"detail": "Not found."}, status=404)

        instance = user_profile
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if getattr(instance, "_prefetched_objects_cache", None):
            # If 'prefetch_related' has been applied to a queryset, we need to
            # forcibly invalidate the prefetch cache on the instance.
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)


class DomainViewSet(viewsets.ModelViewSet):
    """
    Domain View Set
    """

    serializer_class = DomainSerializer
    queryset = Domain.objects.all()
    filter_backends = (filters.SearchFilter,)
    search_fields = ("url", "name")
    http_method_names = ["get", "post", "head"]


class IssueViewSet(viewsets.ModelViewSet):
    """
    Issue View Set
    """

    filter_backends = (filters.SearchFilter,)
    http_method_names = ("get", "post", "head")
    search_fields = ("url", "description", "user__id")
    serializer_class = IssueSerializer

    def get_queryset(self):
        queryset = (
            Issue.objects.exclude(Q(is_hidden=True) & ~Q(user_id=self.request.user.id))
            if self.request.user.is_authenticated
            else Issue.objects.exclude(Q(is_hidden=True))
        )

        status = self.request.GET.get("status")
        if status:
            queryset = queryset.filter(status=status)

        domain_url = self.request.GET.get("domain")
        if domain_url:
            queryset = queryset.filter(domain__url=domain_url)

        return queryset

    def get_issue_info(self, request, issue):
        if issue is None:
            return {}

        # Check if there is an image in the `screenshot` field of the Issue table
        if issue.screenshot:
            # If an image exists in the Issue table, return it along with additional images from IssueScreenshot
            screenshots = [request.build_absolute_uri(issue.screenshot.url)] + [
                request.build_absolute_uri(screenshot.image.url) for screenshot in issue.screenshots.all()
            ]
        else:
            # If no image exists in the Issue table, return only the images from IssueScreenshot
            screenshots = [request.build_absolute_uri(screenshot.image.url) for screenshot in issue.screenshots.all()]

        is_upvoted = False
        is_flagged = False
        if request.user.is_authenticated:
            is_upvoted = request.user.userprofile.issue_upvoted.filter(id=issue.id).exists()
            is_flagged = request.user.userprofile.issue_flaged.filter(id=issue.id).exists()

        tag_serializer = TagSerializer(issue.tags.all(), many=True)
        tags = tag_serializer.data

        return {
            **IssueSerializer(issue).data,
            "closed_by": issue.closed_by.username if issue.closed_by else None,
            "flagged": is_flagged,
            "flags": issue.flaged.count(),
            "screenshots": screenshots,
            "upvotes": issue.upvoted.count(),
            "upvotted": is_upvoted,
            "tags": tags,
        }

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        issues = []
        page = self.paginate_queryset(queryset)
        if page is None:
            return Response(issues)
        for issue in page:
            issues.append(self.get_issue_info(request, issue))
        return self.get_paginated_response(issues)

    def retrieve(self, request, pk, *args, **kwargs):
        return Response(self.get_issue_info(request, Issue.objects.filter(id=pk).first()))

    def create(self, request, *args, **kwargs):
        request.data._mutable = True

        # Since the tags field is json encoded we need to decode it
        tags = None
        try:
            if "tags" in request.data:
                tags_json = request.data.get("tags")
                if isinstance(tags_json, list):
                    tags_json = tags_json[0]
                tags = json.loads(tags_json)

                if isinstance(tags, list) and any(isinstance(i, list) for i in tags):
                    tags = [item for sublist in tags for item in sublist]

                del request.data["tags"]
        except (ValueError, MultiValueDictKeyError) as e:
            return Response({"error": "Invalid tags format."}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            request.data._mutable = False

        screenshot_count = len(self.request.FILES.getlist("screenshots"))
        if screenshot_count == 0:
            return Response({"error": "Upload at least one image!"}, status=status.HTTP_400_BAD_REQUEST)
        elif screenshot_count > 5:
            return Response({"error": "Max limit of 5 images!"}, status=status.HTTP_400_BAD_REQUEST)

        data = super().create(request, *args, **kwargs).data
        issue = Issue.objects.filter(id=data["id"]).first()

        if tags:
            issue.tags.add(*tags)

        for screenshot in self.request.FILES.getlist("screenshots"):
            if image_validator(screenshot):
                filename = screenshot.name
                screenshot.name = f"{filename[:10]}{str(uuid.uuid4())[:40]}.{filename.split('.')[-1]}"
                file_path = default_storage.save(f"screenshots/{screenshot.name}", screenshot)

                # Create the IssueScreenshot object and associate it with the issue
                IssueScreenshot.objects.create(image=file_path, issue=issue)
            else:
                return Response({"error": "Invalid image"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(self.get_issue_info(request, issue))


class LikeIssueApiView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, id, format=None, *args, **kwargs):
        return Response(
            {
                "likes": UserProfile.objects.filter(issue_upvoted__id=id).count(),
            }
        )

    def post(self, request, id, format=None, *args, **kwargs):
        issue = Issue.objects.get(id=id)
        userprof = UserProfile.objects.get(user=request.user)
        if userprof in UserProfile.objects.filter(issue_upvoted=issue):
            userprof.issue_upvoted.remove(issue)
            userprof.save()
            return Response({"issue": "unliked"})
        else:
            userprof.issue_upvoted.add(issue)
            userprof.save()

            liked_user = issue.user
            liker_user = request.user
            issue_pk = issue.pk

            if liked_user:
                msg_plain = render_to_string(
                    "email/issue_liked.html",
                    {
                        "liker_user": liker_user.username,
                        "liked_user": liked_user.username,
                        "issue_pk": issue_pk,
                    },
                )
                msg_html = render_to_string(
                    "email/issue_liked.html",
                    {
                        "liker_user": liker_user.username,
                        "liked_user": liked_user.username,
                        "issue_pk": issue_pk,
                    },
                )

                send_mail(
                    "Your issue got an upvote!!",
                    msg_plain,
                    settings.EMAIL_TO_STRING,
                    [liked_user.email],
                    html_message=msg_html,
                )

            return Response({"issue": "liked"})


class FlagIssueApiView(APIView):
    """
    api for Issue like,flag and bookmark
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, id, format=None, *args, **kwargs):
        return Response(
            {
                "flags": UserProfile.objects.filter(issue_flaged__id=id).count(),
            }
        )

    def post(self, request, id, format=None, *args, **kwargs):
        issue = Issue.objects.get(id=id)
        userprof = UserProfile.objects.get(user=request.user)
        if userprof in UserProfile.objects.filter(issue_flaged=issue):
            userprof.issue_flaged.remove(issue)
            userprof.save()
            return Response({"issue": "unflagged"})
        else:
            userprof.issue_flaged.add(issue)
            userprof.save()
            return Response({"issue": "flagged"})


class UserScoreApiView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, id, format=None, *args, **kwargs):
        total_score = Points.objects.filter(user__id=id).annotate(total_score=Sum("score"))

        return Response({"total_score": total_score})


class LeaderboardApiViewSet(APIView):
    def get_queryset(self):
        return User.objects.all()

    def filter(self, request, *args, **kwargs):
        paginator = PageNumberPagination()
        global_leaderboard = LeaderboardBase()

        month = self.request.query_params.get("month")
        year = self.request.query_params.get("year")

        if not year:
            return Response("Year not passed", status=400)

        elif isinstance(year, str) and not year.isdigit():
            return Response("Invalid year passed", status=400)

        if month:
            if not month.isdigit():
                return Response("Invalid month passed", status=400)

            try:
                date = datetime(int(year), int(month), 1)
            except:
                return Response("Invalid month or year passed", status=400)

        queryset = global_leaderboard.get_leaderboard(month, year, api=True)
        users = []
        rank_user = 1
        for each in queryset:
            temp = {}
            temp["rank"] = rank_user
            temp["id"] = each["id"]
            temp["User"] = each["username"]
            temp["score"] = Points.objects.filter(user=each["id"]).aggregate(total_score=Sum("score"))
            temp["image"] = list(UserProfile.objects.filter(user=each["id"]).values("user_avatar"))[0]
            temp["title_type"] = list(UserProfile.objects.filter(user=each["id"]).values("title"))[0]
            temp["follows"] = list(UserProfile.objects.filter(user=each["id"]).values("follows"))[0]
            temp["savedissue"] = list(UserProfile.objects.filter(user=each["id"]).values("issue_saved"))[0]
            rank_user = rank_user + 1
            users.append(temp)

        page = paginator.paginate_queryset(users, request)
        return paginator.get_paginated_response(page)

    def group_by_month(self, request, *args, **kwargs):
        global_leaderboard = LeaderboardBase()

        year = self.request.query_params.get("year")

        if not year:
            year = datetime.now().year

        if isinstance(year, str) and not year.isdigit():
            return Response(f"Invalid query passed | Year:{year}", status=400)

        year = int(year)

        leaderboard = global_leaderboard.monthly_year_leaderboard(year, api=True)
        month_winners = []

        months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "Novermber",
            "December",
        ]

        for month_indx, usr in enumerate(leaderboard):
            month_winner = {"user": usr, "month": months[month_indx]}
            month_winners.append(month_winner)

        return Response(month_winners)

    def global_leaderboard(self, request, *args, **kwargs):
        paginator = PageNumberPagination()
        global_leaderboard = LeaderboardBase()

        queryset = global_leaderboard.get_leaderboard(api=True)
        page = paginator.paginate_queryset(queryset, request)

        return paginator.get_paginated_response(page)

    def get(self, request, format=None, *args, **kwargs):
        filter = request.query_params.get("filter")
        group_by_month = request.query_params.get("group_by_month")
        leaderboard_type = request.query_params.get("leaderboard_type")

        if filter:
            return self.filter(request, *args, **kwargs)

        elif group_by_month:
            return self.group_by_month(request, *args, **kwargs)
        elif leaderboard_type == "organizations":
            return self.organization_leaderboard(request, *args, **kwargs)
        else:
            return self.global_leaderboard(request, *args, **kwargs)

    def organization_leaderboard(self, request, *args, **kwargs):
        paginator = PageNumberPagination()
        organizations = (
            Organization.objects.values().annotate(issue_count=Count("domain__issue")).order_by("-issue_count")
        )
        page = paginator.paginate_queryset(organizations, request)

        return paginator.get_paginated_response(page)


class StatsApiViewset(APIView):
    def get(self, request, *args, **kwargs):
        bug_count = Issue.objects.all().count()
        user_count = User.objects.all().count()
        hunt_count = Hunt.objects.all().count()
        domain_count = Domain.objects.all().count()

        return Response({"bugs": bug_count, "users": user_count, "hunts": hunt_count, "domains": domain_count})


class UrlCheckApiViewset(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        domain_url = request.data.get("domain_url", None)

        if domain_url is None or domain_url.strip() == "":
            return Response([])

        domain = domain_url.replace("https://", "").replace("http://", "").replace("www.", "")

        issues = (
            Issue.objects.filter(Q(Q(domain__name=domain) | Q(domain__url__icontains=domain)) & Q(is_hidden=False))
            .values(
                "id",
                "description",
                "created__day",
                "created__month",
                "created__year",
                "domain__url",
                "user__userprofile__user_avatar",
            )
            .all()
        )

        return Response(issues[:10])


class BugHuntApiViewset(APIView):
    permission_classes = [AllowAny]

    def get_active_hunts(self, request, fields, *args, **kwargs):
        hunts = (
            Hunt.objects.values(*fields)
            .filter(is_published=True, starts_on__lte=datetime.now(), end_on__gte=datetime.now())
            .order_by("-prize")
        )
        return Response(hunts)

    def get_previous_hunts(self, request, fields, *args, **kwargs):
        hunts = Hunt.objects.values(*fields).filter(is_published=True, end_on__lte=datetime.now()).order_by("-end_on")
        return Response(hunts)

    def get_upcoming_hunts(self, request, fields, *args, **kwargs):
        hunts = (
            Hunt.objects.values(*fields).filter(is_published=True, starts_on__gte=datetime.now()).order_by("starts_on")
        )
        return Response(hunts)

    def get_search_by_name(self, request, search_query, fields, *args, **kwargs):
        hunts = Hunt.objects.values(*fields).filter(is_published=True, name__icontains=search_query).order_by("end_on")
        return Response(hunts)

    def get(self, request, *args, **kwargs):
        activeHunt = request.query_params.get("activeHunt")
        previousHunt = request.query_params.get("previousHunt")
        upcomingHunt = request.query_params.get("upcomingHunt")
        search_query = request.query_params.get("search")
        fields = (
            "id",
            "name",
            "url",
            "prize",
            "logo",
            "banner",
            "description",
            "starts_on",
            "end_on",
        )

        if search_query:
            return self.get_search_by_name(request, search_query, fields, *args, **kwargs)
        elif activeHunt:
            return self.get_active_hunts(request, fields, *args, **kwargs)
        elif previousHunt:
            return self.get_previous_hunts(request, fields, *args, **kwargs)
        elif upcomingHunt:
            return self.get_upcoming_hunts(request, fields, *args, **kwargs)
        hunts = Hunt.objects.values(*fields).filter(is_published=True).order_by("-end_on")
        return Response(hunts)


class BugHuntApiViewsetV2(APIView):
    permission_classes = [AllowAny]

    def serialize_hunts(self, hunts):
        hunts = BugHuntSerializer(hunts, many=True)

        serialize_hunts_list = []

        for hunt in hunts.data:
            hunt_prizes = HuntPrize.objects.filter(hunt__id=hunt["id"])
            hunt_prizes = BugHuntPrizeSerializer(hunt_prizes, many=True)

            serialize_hunts_list.append({**hunt, "prizes": hunt_prizes.data})

        return serialize_hunts_list

    def get_active_hunts(self, request, *args, **kwargs):
        hunts = Hunt.objects.filter(
            is_published=True, starts_on__lte=datetime.now(), end_on__gte=datetime.now()
        ).order_by("-prize")
        return Response(self.serialize_hunts(hunts))

    def get_previous_hunts(self, request, *args, **kwargs):
        hunts = Hunt.objects.filter(is_published=True, end_on__lte=datetime.now()).order_by("-end_on")
        return Response(self.serialize_hunts(hunts))

    def get_upcoming_hunts(self, request, *args, **kwargs):
        hunts = Hunt.objects.filter(is_published=True, starts_on__gte=datetime.now()).order_by("starts_on")
        return Response(self.serialize_hunts(hunts))

    def get(self, request, *args, **kwargs):
        paginator = PageNumberPagination()

        activeHunt = request.query_params.get("activeHunt")
        previousHunt = request.query_params.get("previousHunt")
        upcomingHunt = request.query_params.get("upcomingHunt")
        if activeHunt:
            page = paginator.paginate_queryset(self.get_active_hunts(request, *args, **kwargs), request)

            return paginator.get_paginated_response(page)

        elif previousHunt:
            page = paginator.paginate_queryset(self.get_previous_hunts(request, *args, **kwargs), request)

            return paginator.get_paginated_response(page)

        elif upcomingHunt:
            page = paginator.paginate_queryset(self.get_upcoming_hunts(request, *args, **kwargs), request)

            return paginator.get_paginated_response(page)

        hunts = self.serialize_hunts(Hunt.objects.filter(is_published=True).order_by("-end_on"))
        page = paginator.paginate_queryset(hunts, request)

        return paginator.get_paginated_response(page)


class InviteFriendApiViewset(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        email = request.data.get("email")

        if not email:
            return Response({"error": "Email is required"}, status=400)

        # Check if user already exists
        if User.objects.filter(email=email).exists():
            return Response({"error": "User already exists"}, status=400)

        try:
            current_site = get_current_site(request)
            referral_code, created = InviteFriend.objects.get_or_create(sender=request.user)
            referral_link = f"https://{current_site.domain}/referral/?ref={referral_code.referral_code}"

            # Prepare email content
            subject = f"Join me on {current_site.name}!"
            message = render_to_string(
                "email/invite_friend.html",
                {
                    "sender": request.user.username,
                    "referral_link": referral_link,
                    "site_name": current_site.name,
                    "site_domain": current_site.domain,
                },
            )

            # Send the invitation email
            email_result = send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [email],
                fail_silently=False,
            )

            if email_result == 1:
                return Response(
                    {
                        "success": True,
                        "message": "Invitation sent successfully",
                        "referral_link": referral_link,
                        "email_status": "delivered",
                    }
                )
            else:
                return Response({"error": "Email failed to send", "email_status": "failed"}, status=500)

        except smtplib.SMTPException as e:
            return Response(
                {
                    "error": "Failed to send invitation email for an unexpected reason",
                    "email_status": "error",
                },
                status=500,
            )


class OrganizationViewSet(viewsets.ModelViewSet):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = (IsAuthenticatedOrReadOnly,)
    filter_backends = (filters.SearchFilter,)
    search_fields = ("id", "name")
    http_method_names = ("get", "post", "put")

    @action(detail=True, methods=["get"])
    def repositories(self, request, pk=None):
        """
        Get all repositories for an organization.
        """
        try:
            organization = self.get_object()
            repos = Repo.objects.filter(organization=organization)
            serializer = RepoSerializer(repos, many=True, context={"request": request})
            return Response(serializer.data)
        except Organization.DoesNotExist:
            return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)


class ContributorViewSet(viewsets.ModelViewSet):
    queryset = Contributor.objects.all()
    serializer_class = ContributorSerializer
    http_method_names = ("get", "post", "put")


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    # permission_classes = (IsAuthenticatedOrReadOnly,)
    http_method_names = ("get", "post")

    def create(self, request, *args, **kwargs):
        data = request.data.copy()

        name = data.get("name", "")
        slug = slugify(name)

        contributors = Project.get_contributors(self, data["github_url"])  # get contributors

        serializer = ProjectSerializer(data=data)

        if serializer.is_valid():
            project_instance = serializer.save()
            project_instance.__setattr__("slug", slug)

            # Set contributors
            if contributors:
                project_instance.contributors.set(contributors)

            serializer = ProjectSerializer(project_instance)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def list(self, request, *args, **kwargs):
        projects = Project.objects.prefetch_related("contributors").all()

        project_data = []
        for project in projects:
            contributors_data = []
            for contributor in project.contributors.all():
                contributor_info = ContributorSerializer(contributor)
                contributors_data.append(contributor_info.data)
            contributors_data.sort(key=lambda x: x["contributions"], reverse=True)
            project_info = ProjectSerializer(project).data
            project_info["contributors"] = contributors_data
            project_data.append(project_info)

        return Response(
            {"count": len(project_data), "projects": project_data},
            status=200,
        )

    @action(detail=False, methods=["get"])
    def search(self, request, *args, **kwargs):
        query = request.query_params.get("q", "")
        projects = Project.objects.filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(tags__name__icontains=query)
            | Q(stars__icontains=query)
            | Q(forks__icontains=query)
        ).distinct()

        project_data = []
        for project in projects:
            contributors_data = []
            for contributor in project.contributors.all():
                contributor_info = ContributorSerializer(contributor)
                contributors_data.append(contributor_info.data)
            contributors_data.sort(key=lambda x: x["contributions"], reverse=True)
            project_info = ProjectSerializer(project).data
            project_info["contributors"] = contributors_data
            project_data.append(project_info)

        return Response(
            {"count": len(project_data), "projects": project_data},
            status=200,
        )

    @action(detail=False, methods=["get"])
    def filter(self, request, *args, **kwargs):
        freshness = request.query_params.get("freshness", None)
        stars = request.query_params.get("stars", None)
        forks = request.query_params.get("forks", None)
        tags = request.query_params.get("tags", None)

        projects = Project.objects.all()

        if freshness:
            projects = projects.filter(freshness__icontains=freshness)
        if stars:
            projects = projects.filter(stars__gte=stars)
        if forks:
            projects = projects.filter(forks__gte=forks)
        if tags:
            projects = projects.filter(tags__name__in=tags.split(",")).distinct()

        project_data = []
        for project in projects:
            contributors_data = []
            for contributor in project.contributors.all():
                contributor_info = ContributorSerializer(contributor)
                contributors_data.append(contributor_info.data)
            contributors_data.sort(key=lambda x: x["contributions"], reverse=True)
            project_info = ProjectSerializer(project).data
            project_info["contributors"] = contributors_data
            project_data.append(project_info)

        return Response(
            {"count": len(project_data), "projects": project_data},
            status=200,
        )


class AuthApiViewset(viewsets.ModelViewSet):
    http_method_names = ("delete",)
    permission_classes = (IsAuthenticated,)

    def delete(self, request, *args, **kwargs):
        try:
            token = request.headers["Authorization"].split(" ")
            user = Token.objects.get(key=token[1]).user
            user_data = User.objects.get(username=user)
            user_data.delete()
            return Response({"success": True, "message": "User deleted successfully !!"})
        except Token.DoesNotExist:
            return Response({"success": False, "message": "User does not exists."})
        except User.DoesNotExist:
            return Response({"success": False, "message": "User does not exists."})


class TagApiViewset(viewsets.ModelViewSet):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer


class TimeLogViewSet(viewsets.ModelViewSet):
    queryset = TimeLog.objects.all()
    serializer_class = TimeLogSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        organization_url = self.request.data.get("organization_url")

        try:
            if organization_url:
                parsed_url = urlparse(organization_url)
                normalized_url = parsed_url.netloc + parsed_url.path

                # Normalize the URL in the Company model (remove the protocol if present)
                try:
                    organization = Organization.objects.get(
                        Q(url__iexact=normalized_url)
                        | Q(url__iexact=f"http://{normalized_url}")
                        | Q(url__iexact=f"https://{normalized_url}")
                    )
                except Organization.DoesNotExist:
                    raise ParseError(detail="Organization not found for the given URL.")

            else:
                organization = None

            # Save the TimeLog with the user and organization (if found, or None)
            serializer.save(user=self.request.user, organization=organization)

        except ValidationError as e:
            raise ParseError(detail=str(e))
        except Exception as e:
            raise ParseError(detail="An unexpected error occurred while creating the time log.")

    @action(detail=False, methods=["post"])
    def start(self, request):
        """Starts a new time log"""
        data = request.data
        data["start_time"] = timezone.now()  # Set start time to current tim

        serializer = self.get_serializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"detail": "An unexpected error occurred while starting the time log."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        """Stops the time log and calculates duration"""
        try:
            timelog = self.get_object()
        except ObjectDoesNotExist:
            raise NotFound(detail="Time log not found.")

        timelog.end_time = timezone.now()
        if timelog.start_time:
            timelog.duration = timelog.end_time - timelog.start_time

        try:
            timelog.save()
            return Response(TimeLogSerializer(timelog).data, status=status.HTTP_200_OK)
        except ValidationError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"detail": "An unexpected error occurred while stopping the time log."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ActivityLogViewSet(viewsets.ModelViewSet):
    queryset = ActivityLog.objects.all()
    serializer_class = ActivityLogSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        try:
            serializer.save(user=self.request.user, recorded_at=timezone.now())
        except ValidationError as e:
            raise ParseError(detail=str(e))
        except Exception as e:
            raise ParseError(detail="An unexpected error occurred while creating the activity log.")


class OwaspComplianceChecker(APIView):
    """
    API endpoint to check OWASP project compliance criteria for a given URL
    """

    permission_classes = [AllowAny]

    def check_github_compliance(self, url):
        """Check GitHub-related compliance criteria"""
        try:
            parsed_url = urlparse(url)
            is_github = parsed_url.netloc == "github.com"
            is_owasp_org = parsed_url.path.startswith("/OWASP/")

            return {
                "github_hosted": is_github,
                "under_owasp_org": is_owasp_org,
                "details": {"url_checked": url, "recommendations": []},
            }
        except Exception:
            return {
                "github_hosted": False,
                "under_owasp_org": False,
                "details": {"url_checked": url, "error": "Unable to parse GitHub URL"},
            }

    def check_website_compliance(self, url):
        """Check website-related compliance criteria"""
        try:
            response = requests.get(url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")

            # Check for OWASP mention
            content = soup.get_text().lower()
            has_owasp_mention = "owasp" in content

            # Check for project page link
            owasp_links = [a for a in soup.find_all("a") if "owasp.org" in a.get("href", "")]
            has_project_link = len(owasp_links) > 0

            # Check for up-to-date info
            has_dates = bool(soup.find_all(["time", "date"]))

            return {
                "has_owasp_mention": has_owasp_mention,
                "has_project_link": has_project_link,
                "has_dates": has_dates,
                "details": {"url_checked": url, "recommendations": []},
            }
        except Exception as e:
            return {
                "has_owasp_mention": False,
                "has_project_link": False,
                "has_dates": False,
                "details": {"url_checked": url, "error": str(e)},
            }

    def check_vendor_neutrality(self, url):
        """Check vendor neutrality compliance"""
        try:
            response = requests.get(url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")

            # Look for common paywall terms
            paywall_terms = ["premium", "subscribe", "subscription", "pay", "pricing"]
            content = soup.get_text().lower()
            has_paywall_indicators = any(term in content for term in paywall_terms)

            return {"possible_paywall": has_paywall_indicators, "details": {"url_checked": url, "recommendations": []}}
        except Exception:
            return {
                "possible_paywall": None,
                "details": {"url_checked": url, "error": "Unable to check vendor neutrality"},
            }

    def post(self, request, *args, **kwargs):
        url = request.data.get("url")
        if not url:
            return Response({"error": "URL is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Run all compliance checks
        github_check = self.check_github_compliance(url)
        website_check = self.check_website_compliance(url)
        vendor_check = self.check_vendor_neutrality(url)

        # Compile recommendations
        recommendations = []
        if not github_check["under_owasp_org"]:
            recommendations.append("Project should be hosted under the OWASP GitHub organization")
        if not website_check["has_owasp_mention"]:
            recommendations.append("Website should clearly state it is an OWASP project")
        if not website_check["has_project_link"]:
            recommendations.append("Website should link to the OWASP project page")
        if vendor_check["possible_paywall"]:
            recommendations.append("Check if the project has features behind a paywall")

        report = {
            "url": url,
            "compliance_status": {"github": github_check, "website": website_check, "vendor_neutrality": vendor_check},
            "recommendations": recommendations,
            "overall_status": "needs_improvement" if recommendations else "compliant",
        }

        return Response(report, status=status.HTTP_200_OK)
