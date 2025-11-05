"""
Calculador de Horas según Normativa Argentina
Incluye cálculo de Tardanza y Retiro Anticipado
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo  # stdlib (Python >=3.9)
from config.default_config import DEFAULT_CONFIG
import math
import re

class ArgentineHoursCalculator:
    """Calculador de horas según normativa laboral argentina"""

    def __init__(self):
        self.jornada_completa     = DEFAULT_CONFIG['jornada_completa_horas']
        self.hora_nocturna_inicio = DEFAULT_CONFIG['hora_nocturna_inicio']  # se usa
        self.hora_nocturna_fin    = DEFAULT_CONFIG['hora_nocturna_fin']     # se usa 
        self.sabado_limite        = DEFAULT_CONFIG.get('sabado_limite_hora', 13)
        self.tolerancia_minutos   = DEFAULT_CONFIG['tolerancia_minutos']
        self.fragmento_minutos    = DEFAULT_CONFIG['fragmento_minutos']
        self.holiday_names        = DEFAULT_CONFIG.get('holiday_names', {})
        self.local_tz             = ZoneInfo(
            DEFAULT_CONFIG.get('local_timezone', 'America/Argentina/Buenos_Aires')
        )
        self.extras_al_50         = DEFAULT_CONFIG.get("extras_al_50", 2)  # p.ej. 4 en ARM
        self.restar_llegada_anticipada_de_horas_extras = DEFAULT_CONFIG.get(
            'restar_llegada_anticipada_de_horas_extras', True
        )

        # Flag general de redondeo: podés usar 'redondear_extras' o 'redondear'
        self.redondear_extras = DEFAULT_CONFIG.get(
            'redondear_extras',
            DEFAULT_CONFIG.get('redondear', False)
        )

        self.test = DEFAULT_CONFIG.get('test', True)

    # -------------------- Helpers de parsing / fechas --------------------

    def redondear_extras_a_media_hora(self, horas: float) -> float:
        """
        Redondea horas a múltiplos de 0.5 hs, con esta lógica:
        - Se toman las horas en minutos.
        - Se separan horas enteras y minutos restantes.
        - Si el resto < fragmento_minutos (por defecto 30) -> se descarta.
        - Si el resto >= fragmento_minutos -> se suma 0.5 hs.

        Ejemplos:
            1h05m (1.0833) -> 1.0
            1h54m (1.9)    -> 1.5
            2h00m (2.0)    -> 2.0
        """
        if not horas or horas <= 0:
            return 0.0

        minutos = int(round(horas * 60))
        horas_enteras, resto = divmod(minutos, 60)

        # Usamos fragmento_minutos (por config, normalmente 30) como corte
        corte = getattr(self, "fragmento_minutos", 30)

        if resto >= corte:
            return float(horas_enteras) + 0.5
        else:
            return float(horas_enteras)

    def _maybe_redondear_extras(self, horas: float) -> float:
        """
        Aplica el redondeo a media hora SOLO si el flag de config está en True.
        Si no, devuelve el valor original.
        """
        if not self.redondear_extras:
            return horas
        return self.redondear_extras_a_media_hora(horas)

    def _split_extra_day_hours_at_13(self, shift_start: str, shift_end: str,
                                     extra_day_hours: float) -> Tuple[float, float]:
        """
        Divide las horas extra DIURNAS (extra_day_hours) en:
        - antes de las 13:00
        - después de las 13:00

        Suposición importante:
        - Las horas extra están al FINAL de la jornada (las últimas horas trabajadas).
        """

        if extra_day_hours <= 0:
            return 0.0, 0.0

        try:
            hs = self._only_hhmm(shift_start)
            he = self._only_hhmm(shift_end)
            if not he:
                return 0.0, 0.0

            if not hs:
                hs = he  # fallback

            # Pasar a minutos desde 00:00
            h_ini, m_ini = map(int, hs.split(':'))
            h_fin, m_fin = map(int, he.split(':'))
            start_min = h_ini * 60 + m_ini
            end_min = h_fin * 60 + m_fin

            # Si el fin es <= inicio, asumimos cruce de medianoche
            if end_min <= start_min:
                end_min += 24 * 60

            # Duración del bloque de horas extra diurnas (en minutos)
            extra_min = int(round(extra_day_hours * 60))

            # Bloque de extra diurna al final de la jornada: [extra_start, end_min)
            extra_start = end_min - extra_min

            # Por seguridad, no permitir que el bloque extra arranque antes del inicio real
            if extra_start < start_min:
                extra_start = start_min

            limite_1300 = 13 * 60  # 13:00 en minutos

            before_13_min = 0
            after_13_min = 0

            # Parte del bloque extra ANTES de las 13:00 → [extra_start, min(end_min, limite_1300))
            if extra_start < limite_1300:
                before_13_min = max(0, min(end_min, limite_1300) - extra_start)

            # Parte del bloque extra DESPUÉS de las 13:00 → [max(extra_start, limite_1300), end_min)
            if end_min > limite_1300:
                after_13_min = max(0, end_min - max(extra_start, limite_1300))

            return before_13_min / 60.0, after_13_min / 60.0

        except Exception:
            return 0.0, 0.0

    def redondear75(self, valor: float) -> float:
        """
        Redondea solo cuando el decimal es exactamente .75
        Ejemplos:
            0.75   -> 1.0
            8.75   -> 9.0
            8.20   -> 8.20
            8.50   -> 8.50
        """
        valor = round(valor, 2)  # normalizo a 2 decimales
        if abs(valor % 1 - 0.75) < 1e-9:
            return math.floor(valor) + 1
        return valor

    #"referenceDate": "2025-10-23", 
    def _get_ref_str(self, day_summary: Dict) -> str:
        ref = day_summary.get('referenceDate') or day_summary.get('date') or ''
        return ref[:10]

    def _parse_iso_to_local(self, s: Optional[str]) -> Optional[datetime]:
        """
        Convierte ISO (con 'Z' u offset) a datetime **local** (naive).
        Si no trae tz, se asume local.
        """
        if not s:
            return None
        s = s.replace('Z', '+00:00')  # normalizo 'Z'
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                return dt  # ya está en local
            return dt.astimezone(self.local_tz).replace(tzinfo=None)
        except Exception:
            return None

    # toma los fichajes REALES y los formatea a hora local y los acomoda 
    def _first_entry_pair_local(self, day_summary: Dict) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Toma el primer START y el primer END de entries, los convierte a **local** y
        devuelve (start_local, end_local). Maneja cruce de día si end <= start.
        """
        start_iso = end_iso = None
        for e in (day_summary.get('entries') or []):
            if e.get('type') == 'START' and not start_iso:
                start_iso = e.get('time') or e.get('date')
            elif e.get('type') == 'END' and not end_iso:
                end_iso = e.get('time') or e.get('date')

        s_dt = self._parse_iso_to_local(start_iso[:25] if start_iso else None)
        e_dt = self._parse_iso_to_local(end_iso[:25] if end_iso else None)
        if s_dt and e_dt and e_dt <= s_dt:
            e_dt += timedelta(days=1)  # cruza medianoche
        return s_dt, e_dt

    def _display_from_entries(self, day_summary: Dict) -> Tuple[str, str, str, str]:
        """
        Devuelve (start_date, start_hhmm, end_date, end_hhmm) usando ENTRIES en local.
        Si faltan, devuelve strings vacíos anclados al ref_str.
        """
        ref_str = self._get_ref_str(day_summary)
        s_dt, e_dt = self._first_entry_pair_local(day_summary)
        if not (s_dt and e_dt):
            return ref_str, "", ref_str, ""
        return (
            s_dt.strftime("%Y-%m-%d"),
            s_dt.strftime("%H:%M"),
            e_dt.strftime("%Y-%m-%d"),
            e_dt.strftime("%H:%M"),
        )

    def _get_intervals_from_entries(self, day_summary: Dict) -> List[Tuple[datetime, datetime]]:
        """
        Devuelve [(start_local, end_local)] usando entries.
        (Si quisieras soportar varios pares START/END, expandí acá).
        """
        s_dt, e_dt = self._first_entry_pair_local(day_summary)
        return [(s_dt, e_dt)] if (s_dt and e_dt) else []

    def _get_holiday_name(self, date_str: str, day_summary: Dict) -> Optional[str]:
        # 1) si viene desde la API
        if day_summary.get('holidays'):
            name = (day_summary['holidays'][0] or {}).get('name')
            if name:
                return name
        # 2) si está en el config
        return self.holiday_names.get(date_str)

    # -------------------- Intersecciones / nocturnas --------------------

    def _intersect_hours(self, a_start: datetime, a_end: datetime,
                         b_start: datetime, b_end: datetime) -> float:
        start = max(a_start, b_start)
        end = min(a_end, b_end)
        return max(0.0, (end - start).total_seconds() / 3600)

    def _compute_night_hours_from_intervals(self, intervals: List[Tuple[datetime, datetime]],
                                            ref_dt: datetime) -> float:
        """
        Ventana nocturna anclada al **día de inicio** (ref_dt): 21:00 → 06:00 del día siguiente.
        """
        n_start = ref_dt.replace(hour=self.hora_nocturna_inicio, minute=0, second=0, microsecond=0)
        n_end   = (ref_dt + timedelta(days=1)).replace(hour=self.hora_nocturna_fin, minute=0, second=0, microsecond=0)
        total = 0.0
        for s_dt, e_dt in intervals:
            total += self._intersect_hours(s_dt, e_dt, n_start, n_end)
        return round(total, 2)

    # -------------------- Feriado por FIN local --------------------

    def _crosses_into_holiday_local_end(self, day_summary: Dict,
                                        ref_str: str,
                                        holiday_dates: Set[str]) -> Optional[str]:
        """
        Si el END (en hora LOCAL) cae en un día distinto y ese día es feriado,
        devuelve esa fecha (YYYY-MM-DD). Si no, None.
        """
        _, e_dt = self._first_entry_pair_local(day_summary)
        if not e_dt:
            return None
        end_date_local = e_dt.strftime("%Y-%m-%d")
        if end_date_local != ref_str and end_date_local in holiday_dates:
            return end_date_local
        return None

    # -------------------- Distribución auxiliar (Lun–Vie) --------------------

    def _weekday_distribution(self, hours: float, has_time_off: bool) -> Dict:
        """
        Reparte horas como Lun–Vie: regulares hasta jornada, luego extras 50% hasta
        'extras_al_50' y el resto 100%.
        """
        if hours <= 0:
            return {'regular': 0.0, 'extra50': 0.0, 'extra100': 0.0, 'pending': 0.0}

        regular = min(hours, float(self.jornada_completa))
        extra = max(0.0, hours - float(self.jornada_completa))

        if extra <= self.extras_al_50:
            e50 = extra
            e100 = 0.0
        else:
            e50 = float(self.extras_al_50)
            e100 = extra - float(self.extras_al_50)

        pending = 0.0
        if not has_time_off and hours < self.jornada_completa:
            pending = float(self.jornada_completa) - hours

        return {'regular': regular, 'extra50': e50, 'extra100': e100, 'pending': pending}
    
    # -------------------- Cálculo de Tardanza y Retiro Anticipado --------------------
    
    def _only_hhmm(self, value) -> str:
        """Devuelve 'HH:MM' si lo encuentra dentro de value; si no, ''."""
        if not value:
            return ""
        m = re.search(r'([01]\d|2[0-3]):[0-5]\d', str(value))
        return m.group(0) if m else ""

    def _calcular_tardanza_minutos(self, time_range: str, shift_start: str) -> float:
        """
        Calcula la tardanza en minutos.
        time_range: "08:30 - 17:15"
        shift_start: "2025-01-15 08:55" (puede incluir fecha completa)
        Retorna: minutos de tardanza (0 si llegó a tiempo o antes)
        """
        if not time_range or not shift_start:
            return 0.0
        
        try:
            # Extraer hora de inicio del horario obligatorio
            hora_obligatoria = time_range.split('-')[0].strip()
            h_oblig, m_oblig = map(int, hora_obligatoria.split(':'))
            
            # Extraer hora real de fichada
            hora_real = self._only_hhmm(shift_start)
            if not hora_real:
                return 0.0
            h_real, m_real = map(int, hora_real.split(':'))
            
            # Convertir a minutos totales
            minutos_obligatorio = h_oblig * 60 + m_oblig
            minutos_real = h_real * 60 + m_real
            
            # Calcular tardanza en minutos
            tardanza_mins = minutos_real - minutos_obligatorio
            return max(0.0, float(tardanza_mins))
            
        except Exception:
            return 0.0

    def _calcular_llegada_anticipada_minutos(self, time_range: str, shift_start: str) -> float:
        """
        Calcula la llegada anticipada en minutos.
        time_range: "09:00 - 17:00"
        shift_start: "2025-01-15 08:40" (puede incluir fecha completa)
        Retorna: minutos de llegada anticipada (0 si llegó a tiempo o después).
        """
        if not time_range or not shift_start:
            return 0.0

        try:
            # Hora de inicio obligatoria
            hora_obligatoria = time_range.split('-')[0].strip()
            h_oblig, m_oblig = map(int, hora_obligatoria.split(':'))

            # Hora real de fichada (solo HH:MM)
            hora_real = self._only_hhmm(shift_start)
            if not hora_real:
                return 0.0
            h_real, m_real = map(int, hora_real.split(':'))

            # Minutos totales
            minutos_obligatorio = h_oblig * 60 + m_oblig
            minutos_real = h_real * 60 + m_real

            # Si llegó antes, la diferencia es positiva
            anticipado_mins = minutos_obligatorio - minutos_real
            return max(0.0, float(anticipado_mins))

        except Exception:
            return 0.0

    def _calcular_retiro_anticipado_minutos(self, time_range: str,
                                            shift_start: str,
                                            shift_end: str) -> float:
        """
        Calcula el retiro anticipado en minutos.
        Maneja correctamente turnos nocturnos que cruzan medianoche.
        
        time_range: "08:00 - 16:45"
        shift_start: "2025-01-15 07:45" 
        shift_end: "2025-01-16 03:45" (puede ser del día siguiente)
        Retorna: minutos de retiro anticipado (0 si se fue a tiempo o después)
        """
        if not time_range or not shift_end:
            return 0.0
        
        try:
            # Extraer hora de fin del horario obligatorio
            hora_obligatoria = time_range.split('-')[1].strip()
            h_oblig, m_oblig = map(int, hora_obligatoria.split(':'))
            
            # Extraer fechas y horas de las fichadas
            hora_start = self._only_hhmm(shift_start) if shift_start else None
            hora_end = self._only_hhmm(shift_end)
            
            if not hora_end:
                return 0.0
            
            # Extraer fechas completas si existen
            fecha_start = None
            fecha_end = None
            
            if shift_start and len(shift_start) >= 10:
                try:
                    fecha_start = datetime.strptime(shift_start[:10], '%Y-%m-%d').date()
                except:
                    pass
                    
            if shift_end and len(shift_end) >= 10:
                try:
                    fecha_end = datetime.strptime(shift_end[:10], '%Y-%m-%d').date()
                except:
                    pass
            
            # CASO 1: Si hay fechas y son diferentes (turno nocturno que cruza medianoche)
            # NO calcular retiro anticipado porque es un turno nocturno válido
            if fecha_start and fecha_end and fecha_end > fecha_start:
                return 0.0
            
            # CASO 2: Mismo día o sin información de fecha - calcular normalmente
            h_end, m_end = map(int, hora_end.split(':'))
            
            minutos_obligatorio = h_oblig * 60 + m_oblig
            minutos_real = h_end * 60 + m_end
            
            # Calcular retiro anticipado en minutos
            retiro_mins = minutos_obligatorio - minutos_real
            return max(0.0, float(retiro_mins))
            
        except Exception:
            return 0.0
    

    def _minutos_a_horas(self, minutos: float) -> float:
        """Convierte minutos a horas decimales SIN recortar minutos."""
        return float(minutos) / 60.0

    def _horas_a_minutos(self, horas: float) -> int:
        """Convierte horas decimales a minutos enteros"""
        return int(round(float(horas) * 60))

    # -------------------- Cálculo principal --------------------

    def process_employee_data(self,
                              day_summaries: List[Dict],
                              employee_info: Dict,
                              previous_pending_hours: float = 0,
                              holidays: Optional[Set[str]] = None) -> Dict:

        daily_data: List[Dict] = []
        totals = {
            'total_days_worked': 0.0,
            'total_hours_worked': 0.0,
            'total_regular_hours': 0.0,
            'total_extra_hours_50': 0.0,
            'total_extra_hours_100': 0.0,
            'total_night_hours': 0.0,
            'total_holiday_hours': 0.0,
            'total_pending_hours': float(previous_pending_hours),
            'total_tardanza_horas': 0.0,
            'total_retiro_anticipado_horas': 0.0,
            'total_extra_day_hours': 0.0,
            'total_extra_night_hours': 0.0,
            'total_extra_night_hours_50': 0.0,
            'total_extra_night_hours_100': 0.0,
            'total_holiday_night_hours': 0.0,
        }

        holidays = holidays or set()

        for day_summary in day_summaries:
            is_holiday_api = bool(day_summary.get('holidays'))
            has_time_off   = bool(day_summary.get('timeOffRequests'))
            has_absence    = 'ABSENT' in (day_summary.get('incidences') or [])
            is_rest_day    = not bool(day_summary.get('isWorkday', True))  # FRANCO
            is_workday     = bool(day_summary.get('isWorkday', True))      # Día laboral
            slots          = day_summary.get('timeSlots') or []

            # ---- Modo test: saltar ausencias / licencias / hoy ----
            if self.test is True:
                if has_absence or has_time_off:
                    continue
                date_str_test = (day_summary.get('date') or '')[:10]
                try:
                    date_dt = datetime.strptime(date_str_test, "%Y-%m-%d")
                    today_local = datetime.now(
                        ZoneInfo("America/Argentina/Buenos_Aires")
                    ).date()
                    if date_dt.date() == today_local:
                        continue
                except Exception:
                    pass

            # Día totalmente vacío (franco sin fichada ni horario) → lo ignoramos
            if (
                is_rest_day and              # es franco / domingo
                not is_holiday_api and       # no es feriado
                not has_time_off and         # sin licencia
                not has_absence and          # sin ausencia cargada
                not slots and                # sin horario obligatorio
                not (day_summary.get('entries') or [])  # sin fichadas
            ):
                continue

            # Horario obligatorio tipo "09:00 - 18:00"
            time_range = (
                f"{slots[0]['startTime']} - {slots[0]['endTime']}"
                if slots and slots[0].get('startTime') and slots[0].get('endTime')
                else None
            )

            ref_str = self._get_ref_str(day_summary)
            if not ref_str:
                continue

            ref_dt = datetime.strptime(ref_str, '%Y-%m-%d')
            dow = ref_dt.weekday()  # 0=Lun … 6=Dom
            weekday_name = self.get_day_of_week_spanish(ref_dt)


            # Horas totales trabajadas según la API
            hours_worked = float(
                day_summary.get('hours', {}).get('worked', 0)
                or day_summary.get('totalHours', 0)
                or 0
            )

            # ---- Horarios de turno desde ENTRIES (para display y cálculos) ----
            disp_start_d, disp_start_h, disp_end_d, disp_end_h = self._display_from_entries(day_summary)
            shift_start = " ".join([disp_start_d, disp_start_h]).strip()
            shift_end   = " ".join([disp_end_d, disp_end_h]).strip()

            # ===== TARDANZA / LLEGADA ANTICIPADA / RETIRO ANTICIPADO =====
            tardanza_mins            = self._calcular_tardanza_minutos(time_range, shift_start)
            retiro_mins              = self._calcular_retiro_anticipado_minutos(time_range, shift_start, shift_end)
            llegada_anticipada_mins  = self._calcular_llegada_anticipada_minutos(time_range, shift_start)

            tardanza_horas           = self._minutos_a_horas(tardanza_mins)
            retiro_horas             = self._minutos_a_horas(retiro_mins)
            llegada_anticipada_horas = self._minutos_a_horas(llegada_anticipada_mins)

            # ===== USAR HORAS CATEGORIZADAS DE LA API (REGULAR / EXTRA) =====
            categorized_hours = day_summary.get('categorizedHours', [])
            regular_hours = 0.0
            extra_hours   = 0.0

            for cat_hour in categorized_hours:
                category_name = cat_hour.get('category', {}).get('name', '').upper()
                hours_ch = float(cat_hour.get('hours', 0))

                if category_name == 'REGULAR':
                    regular_hours += hours_ch
                elif category_name == 'EXTRA':
                    extra_hours += hours_ch

            # Redondeo opcional de extras antes de ajustes
            extra_hours = self._maybe_redondear_extras(extra_hours)

            # Descontar llegada anticipada de horas extra (si config lo pide)
            if self.restar_llegada_anticipada_de_horas_extras:
                extra_mins = self._horas_a_minutos(extra_hours)
                extra_mins = max(0, extra_mins - int(llegada_anticipada_mins))
                extra_hours = extra_mins / 60.0

            # Fecha de salida para el reporte
            out_date_str = ref_str

            # ¿Es feriado a efectos de 100%?
            is_holiday_output = is_holiday_api
            holiday_name = None
            if is_holiday_output:
                holiday_name = (
                    self._get_holiday_name(out_date_str, day_summary)
                    or self._get_holiday_name(ref_str, day_summary)
                )

            # Intervalos reales de trabajo y horas nocturnas
            intervals   = self._get_intervals_from_entries(day_summary)
            night_hours = self._compute_night_hours_from_intervals(intervals, ref_dt) \
                          if intervals else 0.0

            # ---- Horas feriado (diurnas + nocturnas) ----
            holiday_hours       = 0.0   # feriado diurnas
            holiday_night_hours = 0.0   # feriado nocturnas

            if is_holiday_output and intervals:
                holiday_night_hours = self._compute_night_hours_from_intervals(intervals, ref_dt)
                total_hours_holiday = hours_worked
                holiday_hours       = max(0.0, total_hours_holiday - holiday_night_hours)

            # ---- Horas pendientes de jornada ----
            pending = 0.0
            if not has_time_off and not has_absence and regular_hours > 0:
                expected_regular = self.jornada_completa
                if regular_hours < expected_regular:
                    pending = expected_regular - regular_hours

            # ================== BUCKETS BASE DE HORAS EXTRA ==================
            # A partir de acá queremos tener:
            #   extra_horas_nocturnas
            #   extra_horas_dia
            #   extra_dsps_de_las_13
            #   extra_horas_feriado

            # 1) Separar extra en diurnas vs nocturnas (21:00–06:00)
            extra_night_hours = min(extra_hours, night_hours)
            extra_day_hours   = max(0.0, extra_hours - extra_night_hours)

            # Buckets “neutros”
            extra_horas_nocturnas = extra_night_hours
            extra_horas_dia       = extra_day_hours
            extra_dsps_de_las_13  = 0.0
            extra_horas_feriado   = 0.0

            # Feriado: todas las horas de feriado (día + noche) en este bucket
            if is_holiday_output:
                extra_horas_feriado = holiday_hours + holiday_night_hours

            # Sábado: separar diurnas antes / después de las 13
            if dow == 5 and extra_horas_dia > 0:
                extra_antes_13, extra_despues_13 = self._split_extra_day_hours_at_13(
                    shift_start, shift_end, extra_horas_dia
                )
                extra_horas_dia      = extra_antes_13
                extra_dsps_de_las_13 = extra_despues_13

            # Para mantener compatibilidad de totales "originales"
            extra_day_hours   = extra_horas_dia + extra_dsps_de_las_13
            extra_night_hours = extra_horas_nocturnas

            # ================== CATEGORIZACIÓN FINAL DE EXTRAS ==================
            extra100              = 0.0
            extra50               = 0.0
            extra_night_hours_50  = 0.0
            extra_night_hours_100 = 0.0

            """
            Reglas (resumen):
            - Extra 50%:    Lun–Vie y sábado antes de las 13 (solo horas adicionales).
            - Extra 100%:   Sábados después de las 13, domingos y feriados (horas adicionales).
            - Franco con fichada: todo lo trabajado se considera 100%.
            - Plus nocturno: 21:00–06:00 (Lunes a Lunes).
            - Extra 50% nocturna: Lun–Vie, adicionales posteriores a las 21:00.
            - Extra 100% nocturna: Sábado, domingo y feriados, adicionales posteriores a las 21:00.
            """

            if dow == 5:  # SÁBADO
                # Diurnas sábado:
                extra50               += extra_horas_dia
                extra100              += extra_dsps_de_las_13
                extra_night_hours_100 += extra_horas_nocturnas

            elif dow == 6 and not is_holiday_output:  # DOMINGO (NO feriado)
                # Domingo común: extras al 100%
                extra100              += extra_horas_dia
                extra_night_hours_100 += extra_horas_nocturnas

            elif is_holiday_output:
                # FERIAdo: ya lo marcamos como Horas Feriado,
                # solo las horas realmente adicionales (si algún día las calculás)
                # deberían ir a extra100.
                # Por ahora no movemos nada a extra100 acá.
                pass

            elif not is_workday:  # FRANCO CON FICHADA
                # Franco con fichada: todo va al 100%
                extra100              += hours_worked
                extra_night_hours_100 += extra_horas_nocturnas

            else:  # LUNES A VIERNES HÁBIL
                extra50              += extra_horas_dia
                extra_night_hours_50 += extra_horas_nocturnas

            # ================== REDONDEO FINAL DE BUCKETS ==================
            # Buckets base
            extra_horas_dia        = self._maybe_redondear_extras(extra_horas_dia)
            extra_dsps_de_las_13   = self._maybe_redondear_extras(extra_dsps_de_las_13)
            extra_horas_nocturnas  = self._maybe_redondear_extras(extra_horas_nocturnas)
            extra_horas_feriado    = self._maybe_redondear_extras(extra_horas_feriado)

            # Buckets de liquidación
            extra_day_hours        = self._maybe_redondear_extras(extra_day_hours)
            extra_night_hours      = self._maybe_redondear_extras(extra_night_hours)
            extra50                = self._maybe_redondear_extras(extra50)
            extra100               = self._maybe_redondear_extras(extra100)
            extra_night_hours_50   = self._maybe_redondear_extras(extra_night_hours_50)
            extra_night_hours_100  = self._maybe_redondear_extras(extra_night_hours_100)
            night_hours            = self._maybe_redondear_extras(night_hours)
            holiday_hours          = self._maybe_redondear_extras(holiday_hours)
            holiday_night_hours    = self._maybe_redondear_extras(holiday_night_hours)

            # ================== ARME DEL REGISTRO DIARIO ==================
            day_record = {
                'date': out_date_str,
                'weekday': dow,
                'is_holiday': bool(is_holiday_output),
                'holiday_name': holiday_name,
                'is_workday': is_workday,
                'is_rest_day': is_rest_day,
                'has_time_off': has_time_off,
                'has_absence': has_absence,
                'time_range': time_range or '',
                'shift_start': shift_start,
                'shift_end': shift_end,
                'hours_worked': round(hours_worked, 2),
                'regular_hours': round(regular_hours, 2),
                'day_of_week': weekday_name,   # <--- NUEVO

                'extra_hours': round(extra_hours),
                'extra_hours_50': round(extra50, 2),
                'extra_hours_100': round(extra100, 2),

                'night_hours': round(night_hours, 2),
                'extra_night_hours_50': round(extra_night_hours_50, 2),
                'extra_night_hours_100': round(extra_night_hours_100, 2),

                'holiday_hours': round(holiday_hours, 2),
                'holiday_night_hours': round(holiday_night_hours, 2),

                'pending_hours': round(pending, 2),

                'tardanza_horas': round(tardanza_horas, 2),
                'retiro_anticipado_horas': round(retiro_horas, 2),
                'llegada_anticipada_horas': round(llegada_anticipada_horas, 2),

                # Nuevos buckets base
                'extra_horas_dia': round(extra_horas_dia, 2),
                'extra_dsps_de_las_13': round(extra_dsps_de_las_13, 2),
                'extra_horas_nocturnas': round(extra_horas_nocturnas, 2),
                'extra_horas_feriado': round(extra_horas_feriado, 2),
            }

            daily_data.append(day_record)

            # ================== ACUMULADORES TOTALES ==================
            if hours_worked > 0:
                totals['total_days_worked'] += 1.0

            totals['total_hours_worked']          += hours_worked
            totals['total_regular_hours']         += regular_hours
            totals['total_extra_hours_50']        += extra50
            totals['total_extra_hours_100']       += extra100
            totals['total_night_hours']           += night_hours
            totals['total_holiday_hours']         += holiday_hours
            totals['total_holiday_night_hours']   += holiday_night_hours
            totals['total_pending_hours']         += pending
            totals['total_tardanza_horas']        += tardanza_horas
            totals['total_retiro_anticipado_horas'] += retiro_horas

            totals['total_extra_day_hours']       += (extra_horas_dia + extra_dsps_de_las_13)
            totals['total_extra_night_hours']     += extra_horas_nocturnas
            totals['total_extra_night_hours_50']  += extra_night_hours_50
            totals['total_extra_night_hours_100'] += extra_night_hours_100

        # Podés redondear totales acá si querés dejarlos prolijos
        for k, v in list(totals.items()):
            totals[k] = round(float(v), 2)

        return {
            'employee_info': employee_info,
            'employee': employee_info,      # alias por si después querés usarlo
            'daily': daily_data,
            'daily_data': daily_data,       # alias para que no rompa nada viejo
            'totals': totals,
        }


    def get_day_of_week_spanish(self, date: datetime) -> str:
        days = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
        return days[date.weekday()]

    def is_night_hour(self, hour: int) -> bool:
        return hour >= self.hora_nocturna_inicio or hour < self.hora_nocturna_fin

    def format_hours(self, hours: float) -> str:
        return '0.00' if hours == 0 else f"{hours:.2f}"

    def format_hours_to_hhmm(self, hours: float) -> str:
        total_minutes = round(hours * 60)
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h:02d}:{m:02d}"

    def minutes_to_hours(self, minutes: int) -> float:
        return round(minutes / 60, 2)

    def round_to_fragment(self, minutes: int) -> int:
        import math
        return math.ceil(minutes / self.fragmento_minutos) * self.fragmento_minutos


# Funciones de compatibilidad
def process_employee_data_from_day_summaries(
    day_summaries: List[Dict],
    employee_info: Dict,
    previous_pending_hours: float = 0,
    period_dates: Dict = None,
    holidays: Optional[Set[str]] = None
) -> Dict:
    calc = ArgentineHoursCalculator()
    return calc.process_employee_data(day_summaries, employee_info, previous_pending_hours, holidays or set())
