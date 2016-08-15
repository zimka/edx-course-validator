# -*- coding: utf-8 -*-
from collections import Counter
from contentstore.course_group_config import GroupConfiguration
from datetime import datetime, timedelta
from django.http import HttpResponse
import json
import logging
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.course_groups.cohorts import get_course_cohorts, get_course_cohort_settings
from xmodule.modulestore.django import modulestore
from .utils import Report, youtube_duration, edx_id_duration, build_items_tree
from models.settings.course_grading import CourseGradingModel
from .settings import *
from django.utils.translation import ugettext as _


class CourseValid():
    """Проверка сценариев и формирование логов"""

    def __init__(self, request, course_key_string):
        self.request = request
        self.store = modulestore()
        self.course_key = CourseKey.from_string(course_key_string)
        self.items = self.store.get_items(self.course_key)
        self.root, self.edges = build_items_tree(self.items)
        self.reports = []


    def validate(self):
        """Запуск всех сценариев проверок"""
        scenarios = [
            "video", "grade", "group", "xmodule",
            "dates", "cohorts", "proctoring",
            "group_visibility", "response_types",
        ]
        results = []
        for sc in scenarios:
            val_name = "val_{}".format(sc)
            validation = getattr(self, val_name)
            report = validation()
            if report is not None:
                results.append(report)
        self.reports = results

    def get_sections_for_rendering(self):
        sections = []
        for r in self.reports:
            sec = {"name": r.name, "passed": not bool(len(r.warnings))}
            if len(r.body):
                sec["output"] = True
                sec["head"] = r.head.split(' - ')
                sec["body"] = [s.split(' - ') for s in r.body]
            else:
                sec["output"] = False

            if not len(r.warnings):
                sec["warnings"] = ["OK"]
            else:
                sec["warnings"] = r.warnings
            sections.append(sec)
        return sections

    def get_HTML_report(self):
        """Формирование отчета из результатов проверок"""
        response = HttpResponse()
        message = ""
        delim = "\n"
        for curr in self.reports:
            message += curr.name + delim
            if len(curr.body):
                message += curr.head + delim
                message += delim.join(curr.body) + delim
            if len(curr.warnings):
                message += delim.join(curr.warnings) + delim
            message += delim

        response.write("<textarea cols='60' rows='60'>")
        response.write(message)
        response.write("</textarea>")
        return response

    def send_log(self):
        """
        Посылает в лог информацию о проверки в виде JSON:
        username, user_email: Данные проверяющего
        datetime: Дата и время
        passed: Пройдены ли без предупреждений проверки:
        warnings: словарь предупреждений (название проверки-предупреждения)
        """
        user = self.request.user
        log = {"username": user.username, "user_email": user.email, "datetime": str(datetime.now())}
        results = []
        passed = True
        for r in self.reports:
            t = ";".join(r.warnings)
            if not len(t):
                t = "OK"
            else:
                passed = False
            results.append({r.name: t})
        log["warnings"] = results
        log["passed"] = passed
        mes = json.dumps(log)
        if passed:
            logging.info(mes)
        else:
            logging.warning(mes)

    def val_video(self):
        """
        Проверка видео: наличие ссылки на YouTube либо edx_video_id.
        При наличии выводится длительнось видео, при отсутствии выводится и
        пишется в отчет
        предупреждение
        """
        items = self.items
        video_items = [i for i in items if i.category == "video"]
        video_strs = []
        report = []
        # Суммирование длительностей всех видео
        total = timedelta()
        for v in video_items:
            mes = ""
            success = 0
            if not (v.youtube_id_1_0) and not (v.edx_video_id):
                mes = _("No source for video '{name}' in '{vertical}' ").\
                    format(name=v.display_name, vertical=v.get_parent().display_name)
                report.append(mes)

            if v.youtube_id_1_0:
                success, cur_mes = youtube_duration(v.youtube_id_1_0)
                if not success:
                    report.append(cur_mes)
                mes = cur_mes

            if v.edx_video_id:
                success, cur_mes = edx_id_duration(v.edx_video_id)
                if not success:
                    report.append(cur_mes)
                mes = cur_mes

            if success:
                total += mes
                if mes>timedelta(seconds=MAX_VIDEO_DURATION):
                    report.append(_("Video {} is longer than 3600 secs").format(v.display_name))

            video_strs.append(u"{} - {}".format(v.display_name, unicode(mes)))

        head = _("Video id - Video duration(sum: {})").format(str(total))
        results = Report(name=_("Video"),
            head=head,
            body=video_strs,
            warnings=report,
            )
        return results

    def val_grade(self):
        """
        Проверка оценок:
        1)совпадение указанного и имеющегося количества заданий в каждой проверяемой категории,
        2)проверка равенства 100 суммы весов категории
        3)Отсутствие в курсе заданий с типом, не указанным в настройках
        """
        report = []
        course_details = CourseGradingModel.fetch(self.course_key)
        graders = course_details.graders
        grade_strs = []
        grade_attributes = ["type", "min_count", "drop_count", "weight"]
        grade_types = []
        grade_nums = []
        grade_weights = []

        # Вытаскиваем типы и количество заданий, прописанных в настройках
        for g in graders:
            grade_strs.append(" - ".join(unicode(g[attr]) for attr in grade_attributes))
            grade_types.append(unicode(g["type"]))
            grade_nums.append(unicode(g["min_count"]))
            try:
                grade_weights.append(float(g["weight"]))
            except ValueError:
                report.append(_("Error occured during weight summation"))

        head = _("Grade name - Grade count - Grade kicked - Grade weight")

        # Проверка суммы весов
        if sum(grade_weights) != 100:
            report.append(_("Tasks weight sum({}) is not equal to 100").format(sum(grade_weights)))

        # Проверка совпадения настроек заданий с материалом курса
        grade_items = [i for i in self.items if i.format is not None]
        for num, key in enumerate(grade_types):
            cur_items = [i for i in grade_items if unicode(i.format) == key]
            if len(cur_items) != int(grade_nums[num]):
                r = _("Task type '{name}': supposed to be {n1}, found in course {n2}").\
                    format(name=key, n1=grade_nums[num], n2=len(cur_items))
                report.append(r)
        # Проверка отсутствия в материале курсе заданий с типом не указанным в настройках
        for item in grade_items:
            if item.format not in grade_types:
                r = _("Task of type '{}' in course, no such task type in grading settings")
                report.append(r)
        results = Report(name=_("Grade"),
            head=head,
            body=grade_strs,
            warnings=report,
            )
        return results

    def val_group(self):
        """Проверка наличия и использования в курсе групп"""
        with self.store.bulk_operations(self.course_key):
            course = self.store.get_course(self.course_key)
            content_group_configuration = GroupConfiguration.get_or_create_content_group(self.store, course)
        groups = content_group_configuration["groups"]

        is_g_used = lambda x: bool(len(x["usage"]))
        # запись для каждой группы ее использования
        group_strs = [u"{} - {}".format(g["name"], is_g_used(g)) for g in groups]
        head = _("Group name - Group used")
        report = []

        results = Report(name=_("Group"),
            head=head,
            body=group_strs,
            warnings=report,
            )
        return results

    def val_xmodule(self):
        """Проверка отсутствия пустых блоков, подсчет количества каждой категории блоков"""
        all_cat_dict = Counter([i.category for i in self.items])
        """
        Все категории разделены на первичные(ниже) и
        вторичные - problems, video, polls итд - записывается в others
        Элементы каждой первичной категории подсчитывается и выводятся.
        Для вторичных категорий выводится только сумма элементов всех
        вторичных категорий
        """
        primary_cat = COUNT_N_CHECK_CAT
        """
        Для additional_count_cat НЕ делается проверка вложенных блоков, но
        делается подсчет элементов
        """
        additional_count_cat = COUNT_NLY_CAT
        secondary_cat = set(all_cat_dict.keys()) - set(primary_cat) \
                        - set(additional_count_cat)

        # Словарь категория:количество для категорий с подробным выводом
        verbose_dict = [(k, all_cat_dict[k]) for k in primary_cat + additional_count_cat]
        # Словарь категория:количество для категорий для элементов без подробного вывода
        silent_dict = {c: all_cat_dict[c] for c in secondary_cat}
        silent_sum = sum(silent_dict.values())

        xmodule_strs = ["{} - {}".format(k, v) for k, v in verbose_dict]
        xmodule_strs.append(_("others - {}").format(silent_sum))
        head = _("Module type - Module count")
        report = []
        # Проверка отсутствия пустых элементов в перв кат кроме additional_count_cat
        check_empty_cat = [x for x in primary_cat]
        primary_items = [i for i in self.items if i.category in check_empty_cat]
        for i in primary_items:
            if not len(i.get_children()):
                s = _("Block '{name}'({cat}) doesn't have any inner blocks or tasks")\
                    .format(name=i.display_name, cat=i.category)
                report.append(s)
        results = Report(name=_("Module"),
            head=head,
            body=xmodule_strs,
            warnings=report
            )
        return results

    def val_dates(self):
        """
        Проверка дат:
        1)Даты старта дочерних блоков больше дат старта блока-родителя
        2)Наличие блоков с датой старта меньше $завтра
        3)Наличие среди стартовавших блоков видимых для студентов
        """
        report = []
        items = self.items
        # Проверка что дата старта child>parent
        for child in items:
            parent = child.get_parent()
            if not parent:
                continue
            if parent.start > child.start:
                mes = _("'{n1}' block has start date {d1}, but his parent '{n2}' has later start date {d2}").\
                    format(n1=child.display_name, d1=child.start,
                    n2=parent.display_name, d2=parent.start)
                report.append(mes)

        # Проверка: Не все итемы имеют дату старта больше сегодня
        tomorrow = datetime.now(items[0].start.tzinfo) + timedelta(days=1)
        items_by_tomorrow = [x for x in items if (x.start < tomorrow and x.category != "course")]

        if not items_by_tomorrow:
            report.append(_("All course release dates are later than {}").format(tomorrow))
        # Проверка: существуют элементы с датой меньше сегодня, видимые для студентов и
        # это не элемент course
        elif all([not self.store.has_published_version(x) for x in items_by_tomorrow]):
            report.append(_("All stuff by tomorrow is not published"))
        elif all([x.visible_to_staff_only for x in items_by_tomorrow]):
            report.append(_("No visible for students stuff by tomorrow"))
        result = Report(name=_("Dates"),
            head=[],
            body=[],
            warnings=report,
            )
        return result

    def val_cohorts(self):
        """Проверка наличия в курсе когорт, для каждой вывод их численности либо сообщение об их отсутствии"""
        course = self.store.get_course(self.course_key)
        cohorts = get_course_cohorts(course)
        names = [getattr(x, "name") for x in cohorts]
        users = [getattr(x, "users").all() for x in cohorts]
        report = []
        cohort_strs = []
        for num, x in enumerate(names):
            cohort_strs.append("{} - {}".format(x, len(users[num])))
        is_cohorted = get_course_cohort_settings(self.course_key).is_cohorted
        if not is_cohorted:
            cohort_strs = []
            report.append(_("Cohorts are disabled"))
        result = Report(name=_("Cohorts"),
            head=_("Cohorts - population"),
            body=cohort_strs,
            warnings=report,
            )
        return result

    def val_proctoring(self):
        """Проверка наличия proctored экзаменов"""
        course = self.store.get_course(self.course_key)
        proctor_strs = [
            _("Available proctoring services - ") + \
            getattr(course, "available_proctoring_services", _("Not defined")),
            _("Proctoring Service - {}").format(getattr(course, "proctoring_service", _("Not defined")))
        ]

        result = Report(name=_("Proctoring"),
            head=_("Parameter - Value"),
            body=proctor_strs,
            warnings=[],
            )
        return result

    def val_group_visibility(self):
        """Составление таблицы видимости элементов для групп"""
        with self.store.bulk_operations(self.course_key):
            course = self.store.get_course(self.course_key)
            content_group_configuration = GroupConfiguration.get_or_create_content_group(self.store, course)
        groups = content_group_configuration["groups"]
        group_names = [g["name"] for g in groups]
        name = _("Items visibility by group")
        head = _("item type - usual student - ") + " - ".join(group_names)
        checked_cats = ["chapter",
             "sequential",
             "vertical",
             "problem",
             "video",
             ]

        get_items_by_type = lambda x: [y for y in self.items if y.category == x]

        # Словарь (категория - итемы)
        cat_items = dict([(t, get_items_by_type(t)) for t in checked_cats])

        # Словарь id группы - название группы
        group_id_dict = dict([(g["id"], g["name"]) for g in groups])

        conf_id = content_group_configuration["id"]
        gv_strs = []
        for cat in checked_cats:
            items = cat_items[cat]
            vis = dict((g, 0) for g in group_names)
            vis["student"] = 0
            for it in items:
                if conf_id not in it.group_access:
                    for key in group_names:
                        vis[key] += 1
                else:
                    ids = it.group_access[conf_id]
                    vis_gn_for_itme = [group_id_dict[i] for i in ids]
                    for gn in vis_gn_for_itme:
                        vis[gn] += 1
                if not it.visible_to_staff_only:
                    vis["student"] += 1

            item_category = "{}({})".format(cat, len(items))
            stud_vis_for_cat = str(vis["student"])

            cat_list = [item_category] + [stud_vis_for_cat] + [str(vis[gn]) for gn in group_names]
            cat_str = " - ".join(cat_list)
            gv_strs.append(cat_str)

        return Report(name=name,
            head=head,
            body=gv_strs,
            warnings=[]
            )

    def val_response_types(self):
        """Считает по всем типам problem количество блоков в курсе"""
        problems = [i for i in self.items if i.category == "problem"]
        # Типы ответов. Взяты из common/lib/capa/capa/tests/test_responsetypes.py
        response_types = ["multiplechoiceresponse",
            "truefalseresponse",
            "imageresponse",
            "symbolicresponse",
            "optionresponse",
            "formularesponse",
            "stringresponse",
            "coderesponse",
            "choiceresponse",
            "javascriptresponse",
            "numericalresponse",
            "customresponse",
            "schematicresponse",
            "annotationresponse",
            "choicetextresponse",
        ]
        response_counts = dict((t, 0) for t in response_types)
        for prob in problems:
            text = prob.get_html()
            out = [prob.display_name]
            for resp in response_types:
                count = text.count("&lt;" + resp) + text.count("<" + resp)
                if count:
                    response_counts[resp] += 1
                    out.append(resp)
        name = _("Response types")
        head = _("type - counts")
        rt_strs = []
        for resp in response_types:
            if response_counts[resp]:
                rt_strs.append("{} - {}".format(resp, response_counts[resp]))
        warnings = []
        if sum(response_counts.values()) != len(problems):
            warnings.append(_("Categorized {counted_num} problems out of {problems_num}").format(
                counted_num=sum(response_counts.values()), problems_num=len(problems)
            ))
        return Report(name=name,
            head=head,
            body=rt_strs,
            warnings=warnings
        )